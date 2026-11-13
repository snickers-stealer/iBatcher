from __future__ import annotations

import base64
import copy
import ctypes
import json
import os
import queue
import random
import re
import secrets
import ssl
import string
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener


APP_NAME = "iBatcher"
VERSION = "1.1.0"
HOST = "127.0.0.1"
TOKEN = secrets.token_urlsafe(24)
STATIC_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "static"


def app_data_dir() -> Path:
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    if getattr(sys, "frozen", False) and os.name == "nt":
        set_folder_icon_attributes(root)
    path = root / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def set_folder_icon_attributes(root: Path) -> None:
    """Restore Explorer folder-icon attributes lost by some ZIP extractors."""
    icon = root / "iBatcher.ico"
    settings = root / "desktop.ini"
    if not icon.is_file() or not settings.is_file():
        return
    kernel = ctypes.windll.kernel32
    for path, flag in ((root, 0x01), (settings, 0x06), (icon, 0x02)):
        current = kernel.GetFileAttributesW(str(path))
        if current != 0xFFFFFFFF:
            kernel.SetFileAttributesW(str(path), current | flag)


REPORTS_DIR = app_data_dir() / "reports"
PROFILES_DIR = app_data_dir() / "profiles"
RUNS_DIR = app_data_dir() / "runs"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
PROFILES_DIR.mkdir(parents=True, exist_ok=True)
RUNS_DIR.mkdir(parents=True, exist_ok=True)


def startup_trace(message: str) -> None:
    if os.environ.get("ABR_STARTUP_TRACE") != "1":
        return
    path = app_data_dir() / "startup.log"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{datetime.now():%H:%M:%S} {message}\n")


DEFAULT_PROFILE = {
    "profileName": "Новый профиль",
    "login": {
        "enabled": True,
        "method": "POST",
        "url": "http://localhost:8081/rest/stateful/personal/login",
        "username": "",
        "password": "",
        "headers": {},
        "body": "",
    },
    "request": {
        "method": "POST",
        "url": "http://localhost:8081/rest/stateful/personal/document/create",
        "username": "",
        "password": "",
        "headers": {"Content-Type": "application/json"},
        "body": "{\n  \"amount\": 10,\n  \"externalId\": \"EXAMPLE\"\n}",
    },
    "dynamicFields": [
        {
            "path": "$.externalId",
            "generator": "uuid32_upper",
            "prefix": "",
            "start": 1,
            "padding": 0,
            "length": 16,
        }
    ],
    "responseValidation": {
        "errorPath": "$.errorCode",
        "errorTextPath": "$.errorText",
        "successPath": "$.id",
    },
    "run": {
        "count": 100,
        "workers": 1,
        "timeout": 60,
        "maxAttempts": 3,
        "verifyTls": True,
        "preflight": True,
        "stopOnError": False,
    },
}


class RunState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.data = self._idle()

    @staticmethod
    def _idle() -> dict[str, Any]:
        return {
            "running": False,
            "phase": "idle",
            "message": "Готов к запуску",
            "total": 0,
            "processed": 0,
            "success": 0,
            "failed": 0,
            "unknown": 0,
            "percent": 0.0,
            "rate": 0.0,
            "elapsed": 0.0,
            "lastError": "",
            "lastResponse": "",
            "preflightDetails": None,
            "reportPath": "",
            "runPath": "",
            "startedAt": "",
            "baselineProcessed": 0,
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            value = dict(self.data)
            if value["running"] and value.get("startedMonotonic"):
                elapsed = max(time.monotonic() - value["startedMonotonic"], 0.001)
                value["elapsed"] = elapsed
                value["rate"] = (value["processed"] - value["baselineProcessed"]) / elapsed
            value.pop("startedMonotonic", None)
            value.pop("baselineProcessed", None)
            return value

    def begin(self, total: int, report_path: Path) -> None:
        with self.lock:
            self.stop_event.clear()
            self.data = self._idle()
            self.data.update(
                {
                    "running": True,
                    "phase": "login",
                    "message": "Выполняется вход",
                    "total": total,
                    "reportPath": str(report_path),
                    "startedAt": datetime.now().isoformat(timespec="seconds"),
                    "startedMonotonic": time.monotonic(),
                }
            )

    def update(self, **values: Any) -> None:
        with self.lock:
            self.data.update(values)

    def add_result(self, success: bool, error: str = "", unknown: bool = False) -> dict[str, Any]:
        with self.lock:
            self.data["processed"] += 1
            self.data["unknown" if unknown else ("success" if success else "failed")] += 1
            if error:
                self.data["lastError"] = error
            total = max(self.data["total"], 1)
            self.data["percent"] = min(self.data["processed"] / total * 100, 100.0)
            elapsed = max(time.monotonic() - self.data["startedMonotonic"], 0.001)
            self.data["elapsed"] = elapsed
            self.data["rate"] = (self.data["processed"] - self.data["baselineProcessed"]) / elapsed
            return dict(self.data)

    def finish(self, phase: str, message: str) -> None:
        with self.lock:
            elapsed = 0.0
            if self.data.get("startedMonotonic"):
                elapsed = time.monotonic() - self.data["startedMonotonic"]
            self.data.update(
                {
                    "running": False,
                    "phase": phase,
                    "message": message,
                    "elapsed": elapsed,
                }
            )


STATE = RunState()


@dataclass
class HttpResult:
    ok: bool
    status: int
    body: str
    error: str = ""
    summary: str = ""


def parse_json_object(raw: str, label: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}: некорректный JSON — {exc.msg}, строка {exc.lineno}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label}: верхний уровень JSON должен быть объектом")
    return value


def normalize_headers(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Заголовки должны быть JSON-объектом")
    return {str(key): str(item) for key, item in value.items() if str(key).strip()}


def basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def ssl_context(verify_tls: bool) -> ssl.SSLContext:
    if verify_tls:
        return ssl.create_default_context()
    return ssl._create_unverified_context()


def make_opener(cookie_jar: CookieJar, verify_tls: bool):
    return build_opener(
        HTTPCookieProcessor(cookie_jar),
        HTTPSHandler(context=ssl_context(verify_tls)),
    )


def encode_body(body: str, headers: dict[str, str]) -> bytes | None:
    if not body:
        return None
    content_type = next(
        (value for key, value in headers.items() if key.lower() == "content-type"),
        "",
    )
    if "json" in content_type.lower():
        parsed = json.loads(body)
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return body.encode("utf-8")


def send_request(
    opener,
    method: str,
    url: str,
    headers: dict[str, str],
    body: str,
    timeout: float,
) -> HttpResult:
    try:
        data = encode_body(body, headers)
        request = Request(url=url, data=data, method=method.upper(), headers=headers)
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            text = raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
            status = response.status
            return HttpResult(200 <= status < 300, status, text)
    except HTTPError as exc:
        raw = exc.read()
        text = raw.decode("utf-8", errors="replace")
        return HttpResult(False, exc.code, text, f"HTTP {exc.code}: {text[:600]}")
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return HttpResult(False, 0, "", f"{type(exc).__name__}: {exc}")


def login(config: dict[str, Any]) -> CookieJar:
    login_config = config["login"]
    jar = CookieJar()
    if not login_config.get("enabled", True):
        return jar

    headers = normalize_headers(login_config.get("headers"))
    username = str(login_config.get("username", ""))
    password = str(login_config.get("password", ""))
    if username or password:
        headers["Authorization"] = basic_auth(username, password)

    opener = make_opener(jar, config["run"]["verifyTls"])
    result = send_request(
        opener,
        login_config.get("method", "POST"),
        login_config["url"],
        headers,
        str(login_config.get("body", "")),
        config["run"]["timeout"],
    )
    if not result.ok:
        raise RuntimeError(f"Ошибка входа: {result.error}")

    parsed = urlparse(login_config["url"])
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}:
        for cookie in jar:
            if cookie.secure:
                cookie.secure = False
    return jar


PATH_PART = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def path_parts(path: str) -> list[str | int]:
    path = path.strip()
    if path.startswith("$."):
        path = path[2:]
    elif path.startswith("$"):
        path = path[1:]
    parts: list[str | int] = []
    for match in PATH_PART.finditer(path):
        key, index = match.groups()
        parts.append(int(index) if index is not None else key)
    if not parts:
        raise ValueError(f"Некорректный путь динамического поля: {path}")
    return parts


def set_path(document: Any, path: str, value: Any) -> None:
    parts = path_parts(path)
    current = document
    for part in parts[:-1]:
        try:
            current = current[part]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Поле {path} не найдено в теле запроса") from exc
    try:
        current[parts[-1]] = value
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Поле {path} не найдено в теле запроса") from exc


MISSING = object()


def get_path(document: Any, path: str) -> Any:
    current = document
    for part in path_parts(path):
        try:
            current = current[part]
        except (KeyError, IndexError, TypeError):
            return MISSING
    return current


def has_value(value: Any) -> bool:
    return value is not MISSING and value is not None and not (
        isinstance(value, str) and not value.strip()
    )


def validate_response(result: HttpResult, validation: dict[str, Any]) -> HttpResult:
    """Turn a transport-level 2xx into a confirmed success or a business error."""
    if not result.ok:
        return result

    error_path = str(validation.get("errorPath", "")).strip()
    error_text_path = str(validation.get("errorTextPath", "")).strip()
    success_path = str(validation.get("successPath", "")).strip()

    parsed: Any = MISSING
    if result.body.strip():
        try:
            parsed = json.loads(result.body)
        except json.JSONDecodeError:
            parsed = MISSING

    if parsed is not MISSING and error_path:
        error_code = get_path(parsed, error_path)
        if has_value(error_code):
            error_text = get_path(parsed, error_text_path) if error_text_path else MISSING
            message = f"Бизнес-ошибка {error_code}"
            if has_value(error_text):
                message += f": {error_text}"
            return HttpResult(False, result.status, result.body, message, message)

    if not success_path:
        return HttpResult(True, result.status, result.body, "", f"HTTP {result.status}: успешно")

    if parsed is MISSING:
        message = (
            f"HTTP {result.status}, но создание не подтверждено: "
            "ответ пустой или не является JSON"
        )
        return HttpResult(False, result.status, result.body, message, message)

    success_value = get_path(parsed, success_path)
    if not has_value(success_value):
        message = (
            f"HTTP {result.status}, но создание не подтверждено: "
            f"поле {success_path} отсутствует или пустое"
        )
        return HttpResult(False, result.status, result.body, message, message)

    details = [f"{success_path}={success_value}"]
    for path, label in (("$.docModule", "docModule"), ("$.docType", "docType")):
        value = get_path(parsed, path)
        if has_value(value):
            details.append(f"{label}={value}")
    summary = "Документ создан: " + ", ".join(details)
    return HttpResult(True, result.status, result.body, "", summary)


def generated_value(rule: dict[str, Any], item_number: int) -> Any:
    generator = rule.get("generator", "uuid32_upper")
    if generator == "uuid32_upper":
        return uuid.uuid4().hex.upper()
    if generator == "uuid32_lower":
        return uuid.uuid4().hex
    if generator == "uuid36":
        return str(uuid.uuid4())
    if generator == "counter":
        start = int(rule.get("start", 1))
        padding = max(int(rule.get("padding", 0)), 0)
        prefix = str(rule.get("prefix", ""))
        number = start + item_number - 1
        return prefix + (str(number).zfill(padding) if padding else str(number))
    if generator == "integer_counter":
        return int(rule.get("start", 1)) + item_number - 1
    if generator == "timestamp_iso":
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if generator == "random_alnum":
        length = min(max(int(rule.get("length", 16)), 1), 512)
        alphabet = string.ascii_uppercase + string.digits
        return "".join(random.SystemRandom().choice(alphabet) for _ in range(length))
    raise ValueError(f"Неизвестный генератор: {generator}")


def render_body(template: dict[str, Any], rules: list[dict[str, Any]], item_number: int) -> str:
    document = copy.deepcopy(template)
    for rule in rules:
        set_path(document, str(rule.get("path", "")), generated_value(rule, item_number))
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


def clone_jar(source: CookieJar) -> CookieJar:
    result = CookieJar()
    for cookie in source:
        result.set_cookie(copy.copy(cookie))
    return result


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("Конфигурация должна быть объектом")
    for section in ("login", "request", "run"):
        if not isinstance(config.get(section), dict):
            raise ValueError(f"Отсутствует секция {section}")

    login_config = config["login"]
    request_config = config["request"]
    run = config["run"]

    if login_config.get("enabled", True) and not str(login_config.get("url", "")).startswith(("http://", "https://")):
        raise ValueError("Укажите полный Login URL")
    if not str(request_config.get("url", "")).startswith(("http://", "https://")):
        raise ValueError("Укажите полный URL основного запроса")

    count = int(run.get("count", 1))
    workers = int(run.get("workers", 1))
    timeout = float(run.get("timeout", 60))
    attempts = int(run.get("maxAttempts", 3))
    if not 1 <= count <= 100_000_000:
        raise ValueError("Количество должно быть от 1 до 100 000 000")
    if not 1 <= workers <= 256:
        raise ValueError("Количество потоков должно быть от 1 до 256")
    if not 1 <= timeout <= 3600:
        raise ValueError("Таймаут должен быть от 1 до 3600 секунд")
    if not 1 <= attempts <= 20:
        raise ValueError("Число попыток должно быть от 1 до 20")

    run["count"] = count
    run["workers"] = workers
    run["timeout"] = timeout
    run["maxAttempts"] = attempts
    run["verifyTls"] = bool(run.get("verifyTls", True))
    run["preflight"] = bool(run.get("preflight", True))
    run["stopOnError"] = bool(run.get("stopOnError", False))

    validation = config.setdefault("responseValidation", {})
    if not isinstance(validation, dict):
        raise ValueError("Настройки проверки ответа должны быть объектом")
    validation["errorPath"] = str(validation.get("errorPath", "$.errorCode")).strip()
    validation["errorTextPath"] = str(validation.get("errorTextPath", "$.errorText")).strip()
    validation["successPath"] = str(validation.get("successPath", "$.id")).strip()
    for label, path in (
        ("Путь поля ошибки", validation["errorPath"]),
        ("Путь текста ошибки", validation["errorTextPath"]),
        ("Путь подтверждения успеха", validation["successPath"]),
    ):
        if path:
            try:
                path_parts(path)
            except ValueError as exc:
                raise ValueError(f"{label}: {exc}") from exc

    login_config["headers"] = normalize_headers(login_config.get("headers"))
    request_config["headers"] = normalize_headers(request_config.get("headers"))
    template = parse_json_object(str(request_config.get("body", "")), "Тело запроса")
    rules = config.get("dynamicFields", [])
    if not isinstance(rules, list):
        raise ValueError("Динамические поля должны быть списком")
    for rule in rules:
        if not str(rule.get("path", "")).strip():
            raise ValueError("У динамического поля не указан путь")
        set_path(copy.deepcopy(template), str(rule["path"]), generated_value(rule, 1))
    return config


def request_headers(config: dict[str, Any]) -> dict[str, str]:
    request_config = config["request"]
    headers = normalize_headers(request_config.get("headers"))
    username = str(request_config.get("username", ""))
    password = str(request_config.get("password", ""))
    if username or password:
        headers["Authorization"] = basic_auth(username, password)
    if not any(key.lower() == "content-type" for key in headers):
        headers["Content-Type"] = "application/json"
    return headers


def create_one(
    opener,
    config: dict[str, Any],
    body: str,
) -> HttpResult:
    request_config = config["request"]
    attempts = config["run"]["maxAttempts"]
    result = HttpResult(False, 0, "", "Запрос не выполнен")
    for attempt in range(1, attempts + 1):
        result = send_request(
            opener,
            request_config.get("method", "POST"),
            request_config["url"],
            request_headers(config),
            body,
            config["run"]["timeout"],
        )
        if result.ok:
            return validate_response(result, config["responseValidation"])
        if request_config.get("method", "POST").upper() == "POST" and (
            result.status == 0 or result.status >= 500
        ):
            return result  # the server may have created the document
        if result.status in {400, 401, 403, 404, 409, 422}:
            return result
        if attempt < attempts and not STATE.stop_event.is_set():
            time.sleep(min(0.5 * (2 ** (attempt - 1)), 8.0))
    return result


def uncertain_result(result: HttpResult) -> bool:
    return not result.ok and (result.status == 0 or result.status >= 500 or 200 <= result.status < 300)


def report_name() -> Path:
    return REPORTS_DIR / f"run_{datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(4)}.txt"


def write_report_header(report, config: dict[str, Any]) -> None:
    safe_config = copy.deepcopy(config)
    safe_config["login"]["password"] = ""
    safe_config["request"]["password"] = ""
    report.write(
        f"{APP_NAME} {VERSION}\n"
        f"Начало: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"Профиль: {config.get('profileName', '')}\n"
        f"URL: {config['request']['url']}\n"
        f"Количество: {config['run']['count']}\n"
        f"Потоки: {config['run']['workers']}\n\n"
    )
    report.flush()


def append_error(report, item_number: int, error: str) -> None:
    report.write(f"ОШИБКА #{item_number}: {error}\n")
    report.flush()


def run_path() -> Path:
    return RUNS_DIR / f"run_{datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(4)}.jsonl"


def read_run(path: Path) -> tuple[dict[str, Any], dict[int, str], str]:
    if path.parent != RUNS_DIR or not path.is_file():
        raise ValueError("Файл прогона не найден")
    with path.open(encoding="utf-8") as stream:
        header = json.loads(stream.readline())
        if header.get("type") != "config":
            raise ValueError("Некорректный файл прогона")
        outcomes: dict[int, str] = {}
        phase = "running"
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                break  # an incomplete final line can follow a crash
            if event.get("type") in {"claimed", "success", "failed", "unknown"}:
                outcomes[int(event["number"])] = event["type"]
            elif event.get("type") == "phase":
                phase = event["value"]
    return header["config"], outcomes, phase


def latest_resumable() -> dict[str, Any] | None:
    for path in sorted(RUNS_DIR.glob("run_*.jsonl"), reverse=True):
        try:
            config, outcomes, phase = read_run(path)
        except (ValueError, KeyError, OSError, json.JSONDecodeError):
            continue
        unknown = sum(value in {"claimed", "unknown"} for value in outcomes.values())
        if phase == "failed" or (phase == "complete" and unknown == 0) or (len(outcomes) >= config["run"]["count"] and unknown == 0):
            continue
        return {
            "file": path.name,
            "profileName": config.get("profileName", ""),
            "total": config["run"]["count"],
            "success": sum(value == "success" for value in outcomes.values()),
            "failed": sum(value == "failed" for value in outcomes.values()),
            "unknown": unknown,
            "remaining": config["run"]["count"] - len(outcomes),
        }
    return None


class RunJournal:
    def __init__(self, path: Path, config: dict[str, Any], resume: bool = False) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.stream = path.open("a" if resume else "x", encoding="utf-8")
        if not resume:
            self.record({"type": "config", "config": public_profile(config)})

    def record(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.stream.flush()
            os.fsync(self.stream.fileno())

    def close(self) -> None:
        self.stream.close()


def run_job(config: dict[str, Any], resume_path: Path | None = None) -> None:
    report_path = report_name()
    STATE.begin(config["run"]["count"], report_path)
    journal_path = resume_path or run_path()
    journal = RunJournal(journal_path, config, resume=resume_path is not None)
    STATE.update(runPath=str(journal_path))
    outcomes: dict[int, str] = {}
    if resume_path:
        _, outcomes, _ = read_run(journal_path)
        success = sum(value == "success" for value in outcomes.values())
        failed = sum(value == "failed" for value in outcomes.values())
        unknown = sum(value in {"claimed", "unknown"} for value in outcomes.values())
        processed = success + failed + unknown
        STATE.update(success=success, failed=failed, unknown=unknown,
                     processed=processed, baselineProcessed=processed,
                     percent=processed / config["run"]["count"] * 100)
    template = parse_json_object(config["request"]["body"], "Тело запроса")
    rules = config.get("dynamicFields", [])
    run = config["run"]

    with report_path.open("w", encoding="utf-8") as report:
        write_report_header(report, config)
        try:
            cookies = login(config)
            cookie_names = ", ".join(cookie.name for cookie in cookies) or "нет"
            STATE.update(phase="preflight", message=f"Вход выполнен; cookies: {cookie_names}")
            report.write(f"Вход выполнен; cookies: {cookie_names}\n")

            next_number = max(outcomes, default=0) + 1
            if run["preflight"] and not resume_path:
                jar = clone_jar(cookies)
                opener = make_opener(jar, run["verifyTls"])
                body = render_body(template, rules, 1)
                journal.record({"type": "claimed", "number": 1})
                result = create_one(opener, config, body)
                uncertain = uncertain_result(result)
                journal.record({"type": "success" if result.ok else ("unknown" if uncertain else "failed"), "number": 1,
                                "error": result.error[:1000]})
                STATE.update(preflightDetails={
                    "requestBody": body,
                    "status": result.status,
                    "responseBody": result.body,
                    "summary": result.summary or result.error,
                    "ok": result.ok,
                })
                response_text = result.body.strip()
                if response_text:
                    report.write(f"Проверочный ответ: {response_text[:4000]}\n")
                STATE.update(lastResponse=result.summary if result.ok else "")
                state = STATE.add_result(result.ok, result.error, uncertain)
                if result.ok:
                    report.write(
                        f"Проверочный запрос: HTTP {result.status}; "
                        f"{result.summary or 'успешно'}\n"
                    )
                    next_number = 2
                else:
                    append_error(report, 1, result.error)
                    STATE.finish("failed", "Проверочный запрос завершился ошибкой")
                    journal.record({"type": "phase", "value": "review" if uncertain else "failed"})
                    report.write("\nИТОГ: массовый запуск отменён после проверочного запроса.\n")
                    return

            STATE.update(phase="running", message="Выполняются запросы")
            counter = {"next": next_number}
            counter_lock = threading.Lock()
            report_lock = threading.Lock()

            def worker() -> None:
                jar = clone_jar(cookies)
                opener = make_opener(jar, run["verifyTls"])
                while not STATE.stop_event.is_set():
                    with counter_lock:
                        item_number = counter["next"]
                        if item_number > run["count"]:
                            return
                        journal.record({"type": "claimed", "number": item_number})
                        counter["next"] += 1
                    try:
                        body = render_body(template, rules, item_number)
                        result = create_one(opener, config, body)
                    except Exception as exc:  # keep a single bad item visible in the report
                        result = HttpResult(False, 0, "", f"{type(exc).__name__}: {exc}")
                    uncertain = uncertain_result(result)
                    journal.record({"type": "success" if result.ok else ("unknown" if uncertain else "failed"),
                                    "number": item_number, "error": result.error[:1000]})
                    STATE.add_result(result.ok, result.error, uncertain)
                    if not result.ok:
                        with report_lock:
                            append_error(report, item_number, result.error)
                        if run["stopOnError"]:
                            STATE.stop_event.set()
                            return

            threads = [threading.Thread(target=worker, daemon=True) for _ in range(run["workers"])]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            snapshot = STATE.snapshot()
            stopped = STATE.stop_event.is_set() and snapshot["processed"] < run["count"]
            phase = "paused" if stopped else ("review" if snapshot["unknown"] else "complete")
            message = "Пауза — можно продолжить" if stopped else (
                f"Завершено; проверьте {snapshot['unknown']} запросов с неизвестным результатом"
                if snapshot["unknown"] else "Выполнение завершено"
            )
            STATE.finish(phase, message)
            journal.record({"type": "phase", "value": phase})
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            STATE.update(lastError=error)
            STATE.finish("failed", "Запуск завершился ошибкой")
            report.write(f"ОШИБКА ЗАПУСКА: {error}\n")
        finally:
            journal.close()
            snapshot = STATE.snapshot()
            report.write(
                "\nИТОГ\n"
                f"Окончание: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"Статус: {snapshot['message']}\n"
                f"Обработано: {snapshot['processed']} / {snapshot['total']}\n"
                f"Успешно: {snapshot['success']}\n"
                f"Ошибок: {snapshot['failed']}\n"
                f"Результат неизвестен: {snapshot['unknown']}\n"
                f"Длительность, сек.: {snapshot['elapsed']:.1f}\n"
            )


def public_profile(config: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(config)
    value.setdefault("login", {})["password"] = ""
    value.setdefault("request", {})["password"] = ""
    return value


def safe_profile_name(name: str) -> str:
    value = re.sub(r"[^\w\- ]+", "", name, flags=re.UNICODE).strip()
    if not value:
        raise ValueError("Укажите название профиля")
    return value[:80]


def profile_path(name: str) -> Path:
    return PROFILES_DIR / f"{safe_profile_name(name)}.json"


class Handler(BaseHTTPRequestHandler):
    server_version = "iBatcher/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def authorized(self) -> bool:
        return self.headers.get("X-App-Token") == TOKEN

    def json_response(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> Any:
        size = int(self.headers.get("Content-Length", "0"))
        if size > 5_000_000:
            raise ValueError("Запрос слишком большой")
        return json.loads(self.rfile.read(size).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/logo.svg":
            raw = (STATIC_DIR / "logo.svg").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if parsed.path == "/":
            page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            page = page.replace("__APP_TOKEN__", TOKEN).replace("__APP_VERSION__", VERSION)
            raw = page.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        if parsed.path == "/api/status" and self.authorized():
            self.json_response(200, STATE.snapshot())
            return
        if parsed.path == "/api/resumable" and self.authorized():
            self.json_response(200, {"run": latest_resumable()})
            return
        if parsed.path == "/api/profiles" and self.authorized():
            names = sorted(path.stem for path in PROFILES_DIR.glob("*.json"))
            self.json_response(200, {"profiles": names, "default": DEFAULT_PROFILE})
            return
        if parsed.path.startswith("/api/profile/") and self.authorized():
            name = unquote(parsed.path.removeprefix("/api/profile/"))
            try:
                data = json.loads(profile_path(name).read_text(encoding="utf-8"))
                self.json_response(200, public_profile(data))
            except Exception as exc:
                self.json_response(404, {"error": str(exc)})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self.authorized():
            self.json_response(403, {"error": "Недействительный локальный токен"})
            return
        try:
            if self.path == "/api/start":
                if STATE.snapshot()["running"]:
                    raise ValueError("Прогон уже выполняется")
                config = validate_config(self.read_json())
                thread = threading.Thread(target=run_job, args=(config,), daemon=True)
                thread.start()
                self.json_response(202, {"ok": True})
                return
            if self.path == "/api/resume":
                if STATE.snapshot()["running"]:
                    raise ValueError("Прогон уже выполняется")
                request = self.read_json()
                name = Path(str(request.get("file", ""))).name
                path = RUNS_DIR / name
                config, outcomes, phase = read_run(path)
                if phase in {"complete", "failed"} or len(outcomes) >= config["run"]["count"]:
                    raise ValueError("Этот прогон уже завершён")
                config["login"]["password"] = str(request.get("loginPassword", ""))
                config["request"]["password"] = str(request.get("requestPassword", ""))
                config = validate_config(config)
                threading.Thread(target=run_job, args=(config, path), daemon=True).start()
                self.json_response(202, {"ok": True})
                return
            if self.path == "/api/stop":
                STATE.stop_event.set()
                STATE.update(message="Пауза после текущих запросов")
                self.json_response(200, {"ok": True})
                return
            if self.path == "/api/save-profile":
                config = self.read_json()
                name = safe_profile_name(str(config.get("profileName", "")))
                config = public_profile(config)
                profile_path(name).write_text(
                    json.dumps(config, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                self.json_response(200, {"ok": True, "name": name})
                return
            if self.path == "/api/open-reports":
                subprocess.Popen(["explorer", str(REPORTS_DIR)])
                self.json_response(200, {"ok": True})
                return
            if self.path == "/api/shutdown":
                if STATE.snapshot()["running"]:
                    raise ValueError("Сначала остановите текущий прогон")
                self.json_response(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.json_response(400, {"error": str(exc)})


def main() -> None:
    startup_trace("main entered")
    server = ThreadingHTTPServer((HOST, 0), Handler)
    startup_trace(f"server created on {server.server_address}")
    port = server.server_address[1]
    url = f"http://{HOST}:{port}/?token={TOKEN}"
    if os.environ.get("ABR_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    startup_trace("starting serve_forever")
    print(f"{APP_NAME} {VERSION}: {url}")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
