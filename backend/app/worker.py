"""Single worker with SQLite lease, subprocess cancellation and bounded runtime."""
from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import time
from uuid import uuid4

from app.jobs import claim, execute_job, fail_job
from app.repositories import now
from app.storage import connection, migrate

log = logging.getLogger(__name__)


def register(worker_id: str) -> bool:
    with connection(write=True) as db:
        row = db.execute("SELECT * FROM worker_state WHERE name='main'").fetchone()
        stale = int(os.getenv("WORKER_STALE_SECONDS", "30"))
        if row and row["heartbeat"] > time.time() - stale and row["worker_id"] != worker_id:
            return False
        db.execute("INSERT INTO worker_state VALUES('main',?,?) ON CONFLICT(name) DO UPDATE SET worker_id=excluded.worker_id,heartbeat=excluded.heartbeat",
                   (worker_id, time.time()))
        db.execute("UPDATE jobs SET status='failed',updated_at=?,error=? WHERE status='running'",
                   (now(), "Расчёт прерван перезапуском сервиса. Запустите его повторно."))
    return True


def heartbeat(worker_id: str) -> bool:
    with connection(write=True) as db:
        return db.execute("UPDATE worker_state SET heartbeat=? WHERE name='main' AND worker_id=?", (time.time(), worker_id)).rowcount == 1


def stop_process(process) -> None:
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
    process.close()


def run() -> None:
    migrate()
    worker_id = uuid4().hex
    if not register(worker_id):
        raise RuntimeError("Another calculation worker is active")
    stopping = False

    def request_stop(*_):
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, request_stop)
    context = multiprocessing.get_context("spawn")
    process, job_id, started = None, None, 0.0
    try:
        while not stopping:
            if not heartbeat(worker_id):
                break
            if process is None:
                job_id = claim(worker_id)
                if job_id:
                    process = context.Process(target=execute_job, args=(job_id, worker_id), daemon=True)
                    process.start()
                    started = time.monotonic()
            else:
                with connection() as db:
                    row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                timed_out = time.monotonic() - started >= int(os.getenv("JOB_TIMEOUT_SECONDS", "600"))
                if timed_out:
                    fail_job(job_id, worker_id, "Расчёт превысил допустимое время. Уменьшите выборку и повторите попытку.")
                if timed_out or row is None or row["status"] != "running" or not process.is_alive():
                    stop_process(process)
                    fail_job(job_id, worker_id, "Процесс расчёта завершился без результата. Повторите попытку.")
                    process = None
            time.sleep(1)
    finally:
        if process is not None:
            fail_job(job_id, worker_id, "Расчёт прерван остановкой сервиса. Запустите его повторно.")
            stop_process(process)
        with connection(write=True) as db:
            db.execute("DELETE FROM worker_state WHERE name='main' AND worker_id=?", (worker_id,))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
