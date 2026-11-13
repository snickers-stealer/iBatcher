import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import app


class MockHandler(BaseHTTPRequestHandler):
    ids = set()
    lock = threading.Lock()

    def log_message(self, *args):
        return

    def do_POST(self):
        if self.path == "/login":
            expected = "Basic " + base64.b64encode(b"mock_user:mock_password").decode()
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Set-Cookie", "JSESSIONID=test-session; Path=/; Secure; HttpOnly; SameSite=None")
            self.end_headers()
            return

        if self.path == "/create":
            if "JSESSIONID=test-session" not in self.headers.get("Cookie", ""):
                self.send_response(401)
                self.end_headers()
                return
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size).decode("utf-8"))
            external_id = body["externalId"]
            with self.lock:
                if external_id in self.ids:
                    self.send_response(409)
                    self.end_headers()
                    return
                self.ids.add(external_id)
            payload = json.dumps({"id": len(self.ids)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/business-error":
            payload = json.dumps(
                {"errorCode": "3011", "errorText": "Идентификатор должен быть уникальным"},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/unconfirmed":
            payload = json.dumps({"status": "accepted"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(404)
        self.end_headers()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    config = {
        "profileName": "Mock test",
        "login": {
            "enabled": True,
            "method": "POST",
            "url": base + "/login",
            "username": "mock_user",
            "password": "mock_password",
            "headers": {},
            "body": "",
        },
        "request": {
            "method": "POST",
            "url": base + "/create",
            "username": "",
            "password": "",
            "headers": {"Content-Type": "application/json"},
            "body": '{"amount":10,"externalId":"template"}',
        },
        "dynamicFields": [{"path": "$.externalId", "generator": "uuid32_upper"}],
        "responseValidation": {
            "errorPath": "$.errorCode",
            "errorTextPath": "$.errorText",
            "successPath": "$.id",
        },
        "run": {
            "count": 12,
            "workers": 3,
            "timeout": 5,
            "maxAttempts": 2,
            "verifyTls": True,
            "preflight": True,
            "stopOnError": True,
        },
    }
    app.run_job(app.validate_config(config))
    result = app.STATE.snapshot()
    assert result["phase"] == "complete", result
    assert result["success"] == 12, result
    assert result["failed"] == 0, result
    assert len(MockHandler.ids) == 12

    business_error_config = json.loads(json.dumps(config))
    business_error_config["request"]["url"] = base + "/business-error"
    business_error_config["run"]["count"] = 1
    app.run_job(app.validate_config(business_error_config))
    result = app.STATE.snapshot()
    assert result["phase"] == "failed", result
    assert result["success"] == 0 and result["failed"] == 1, result
    assert "Бизнес-ошибка 3011" in result["lastError"], result

    unconfirmed_config = json.loads(json.dumps(config))
    unconfirmed_config["request"]["url"] = base + "/unconfirmed"
    unconfirmed_config["run"]["count"] = 1
    app.run_job(app.validate_config(unconfirmed_config))
    result = app.STATE.snapshot()
    server.shutdown()
    assert result["phase"] == "failed", result
    assert "поле $.id отсутствует" in result["lastError"], result
    print("Mock flow OK: success, business error and unconfirmed HTTP 200")


if __name__ == "__main__":
    main()
