"""Confirm the actual uvicorn process records safe application request logs."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import Request, urlopen
from tests.process_utils import NO_WINDOW, terminate_owned_process


class RequestLoggingTests(unittest.TestCase):
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
