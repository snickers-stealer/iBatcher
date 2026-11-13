import copy
import tempfile
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import patch

import app


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        runs = root / "runs"
        reports = root / "reports"
        runs.mkdir()
        reports.mkdir()
        config = copy.deepcopy(app.DEFAULT_PROFILE)
        config["login"]["enabled"] = False
        config["run"].update(count=8, workers=1, preflight=False, maxAttempts=1)
        config = app.validate_config(config)
        sent = []

        def create(_, __, body):
            sent.append(body)
            if len(sent) == 3:
                app.STATE.stop_event.set()
            return app.HttpResult(True, 200, '{"id":"ok"}', summary="id=ok")

        with patch.object(app, "RUNS_DIR", runs), patch.object(app, "REPORTS_DIR", reports), \
             patch.object(app, "login", return_value=CookieJar()), \
             patch.object(app, "create_one", side_effect=create):
            app.run_job(config)
            paused = app.STATE.snapshot()
            assert paused["phase"] == "paused" and paused["success"] == 3, paused
            path = Path(paused["runPath"])
            saved, outcomes, phase = app.read_run(path)
            assert saved["login"]["password"] == ""
            assert len(outcomes) == 3 and phase == "paused"

            with patch.object(app, "create_one", return_value=app.HttpResult(True, 200, '{"id":"ok"}')):
                app.run_job(app.validate_config(saved), path)
            done = app.STATE.snapshot()
            assert done["phase"] == "complete" and done["success"] == 8, done
            assert app.latest_resumable() is None

            crash = app.RunJournal(runs / "run_crash.jsonl", config)
            crash.record({"type": "claimed", "number": 1})
            crash.record({"type": "success", "number": 1})
            crash.record({"type": "claimed", "number": 2})
            crash.close()
            interrupted = app.latest_resumable()
            assert interrupted["unknown"] == 1 and interrupted["success"] == 1
            with patch.object(app, "create_one", return_value=app.HttpResult(True, 200, '{"id":"ok"}')):
                app.run_job(config, runs / interrupted["file"])
            recovered = app.STATE.snapshot()
            assert recovered["unknown"] == 1 and recovered["success"] == 7, recovered

    print("Pause, resume and uncertain requests OK")


if __name__ == "__main__":
    main()
