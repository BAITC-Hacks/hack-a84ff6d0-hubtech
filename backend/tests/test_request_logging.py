"""Confirm the actual uvicorn process records safe application request logs."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request, urlopen
from fastapi.testclient import TestClient
from tests.process_utils import NO_WINDOW, terminate_owned_process


class RequestLoggingTests(unittest.TestCase):
    def test_internal_exception_is_handled_before_asgi_server_logs_its_contents(self):
        from app.main import app
        from app.security import current_session

        def session():
            return {"user": {"id": "log-test", "role": "manager"}}

        marker = "private-sale-details-must-not-be-logged"
        previous = dict(app.dependency_overrides)
        app.dependency_overrides[current_session] = session
        try:
            # No lifespan is needed: the failing repository is replaced and
            # the test must not touch the workstation's database.
            with patch("app.api.routes.repo.list_orders", side_effect=RuntimeError(marker)):
                with self.assertLogs("umytpa", level="INFO") as captured:
                    response = TestClient(app, raise_server_exceptions=True).get(
                        "/api/orders", headers={"X-Request-ID": "safe-error-test"})
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.headers["X-Request-ID"], "safe-error-test")
            self.assertNotIn(marker, response.text)
            self.assertNotIn(marker, "\n".join(captured.output))
            self.assertIn("error_type=RuntimeError", "\n".join(captured.output))
            self.assertIn("route=/api/orders status=500", "\n".join(captured.output))
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)

    def test_info_request_log_is_emitted_without_uvicorn_access_log(self):
        with tempfile.TemporaryDirectory() as folder:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            environment = dict(os.environ, APP_DB_PATH=folder + "/logs.sqlite3", ORDER_DB_PATH=folder + "/logs.sqlite3",
                               APP_ENV="production", APP_ORIGIN="", PYTHONIOENCODING="utf-8")
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                                        "--port", str(port), "--no-access-log"], cwd=Path(__file__).resolve().parents[1],
                                       env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
            try:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        request = Request(f"http://127.0.0.1:{port}/api/health?password=must-not-be-logged",
                                          headers={"X-Request-ID": "logging-smoke-id"})
                        with urlopen(request, timeout=2) as response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.headers["X-Request-ID"], "logging-smoke-id")
                        break
                    except URLError:
                        if time.monotonic() >= deadline or process.poll() is not None:
                            self.fail("API did not start for request logging test")
                        time.sleep(0.1)
                time.sleep(0.1)
            finally:
                terminate_owned_process(process)
                try:
                    output, _ = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    output, _ = process.communicate(timeout=10)
            logs = output.decode("utf-8", errors="replace")
            self.assertIn("request_id=logging-smoke-id", logs)
            self.assertIn("route=/api/health status=200 duration_ms=", logs)
            self.assertNotIn("must-not-be-logged", logs)


if __name__ == "__main__":
    unittest.main()
