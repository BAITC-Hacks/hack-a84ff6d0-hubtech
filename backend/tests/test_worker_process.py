"""Exercise real OS subprocess termination and worker recovery against SQLite."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import jobs
from app.repositories import create_user
from app.schemas import RecommendRequest
from app.storage import connection, migrate
from app.worker import register
from tests.process_utils import NO_WINDOW, terminate_owned_process


class WorkerProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        database = self.directory.name + "/worker.sqlite3"
        env = patch.dict(os.environ, {"APP_DB_PATH": database, "ORDER_DB_PATH": database,
                                     "DATA_SOURCE": "synthetic", "APP_ENV": "development",
                                     "OPENAI_API_KEY": "", "JOB_TIMEOUT_SECONDS": "1"})
        env.start()
        self.addCleanup(env.stop)
        migrate()
        self.user = create_user("worker-user", "worker-test-password", "admin")

    def start_worker(self, timeout="1"):
        environment = dict(os.environ, JOB_TIMEOUT_SECONDS=timeout)
        process = subprocess.Popen([sys.executable, "-m", "app.worker"], env=environment,
                                   cwd=Path(__file__).resolve().parents[1], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=NO_WINDOW)
        self.addCleanup(self.stop_worker, process)
        self.wait_until(lambda: jobs.worker_available(), 20)
        self.assertIsNone(process.poll())
        return process

    def stop_worker(self, process):
        if process.poll() is None:
            terminate_owned_process(process)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    def wait_until(self, predicate, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        self.fail("Worker did not reach expected state before test deadline")

    def test_actual_subprocess_timeout_cannot_publish_late_order(self):
        # An already-expired deadline is deterministic on fast and slow hosts.
        # A 1-second budget let the synthetic calculation finish legitimately.
        process = self.start_worker(timeout="0")
        job_id = jobs.enqueue(RecommendRequest(), self.user, "timeout-job")["job_id"]
        self.wait_until(lambda: jobs.get_job(job_id, self.user)["status"] == "failed")
        time.sleep(2)
        result = jobs.get_job(job_id, self.user)
        self.assertIsNone(result["order_id"])
        self.assertIn("время", result["error"])
        self.assertIsNone(process.poll())
        with connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)

    def test_new_worker_recovers_orphan_and_finishes_durable_queue(self):
        # This is the exact durable state left by an abruptly stopped worker.
        register("stopped-worker")
        orphan = jobs.enqueue(RecommendRequest(), self.user, "orphan")["job_id"]
        jobs.claim("stopped-worker")
        second_user = create_user("queued-user", "queued-test-password", "manager")
        queued = jobs.enqueue(RecommendRequest(), second_user, "queued")["job_id"]
        with connection(write=True) as db:
            db.execute("UPDATE worker_state SET heartbeat=0")
        self.start_worker(timeout="30")
        self.wait_until(lambda: jobs.get_job(queued, second_user)["status"] in ("completed", "failed"), 40)
        self.assertEqual(jobs.get_job(orphan, self.user)["status"], "failed")
        result = jobs.get_job(queued, second_user)
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertIsNotNone(result["order_id"])
        time.sleep(1.2)  # Let the supervisor reap the completed child before stopping it.


if __name__ == "__main__":
    unittest.main()
