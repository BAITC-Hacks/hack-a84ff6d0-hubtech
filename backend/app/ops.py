"""SQLite online backups and explicit offline restore using shared app settings."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone

from app.storage import database_path

BACKUP_NAME = re.compile(r"^umytpa-\d{8}T\d{12}Z\.sqlite3$")


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def _verify(path: Path) -> None:
    with closing(_readonly(path)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Backup failed SQLite integrity check")
        if not connection.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
            raise ValueError("Database has no application tables")


def _copy_database(source: Path, target: Path) -> None:
    deadline = time.monotonic() + 120

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite backup exceeded 120 seconds")

    with closing(_readonly(source)) as source_db, closing(sqlite3.connect(target)) as target_db:
        source_db.backup(target_db, pages=256, progress=progress, sleep=.1)
        target_db.execute("PRAGMA journal_mode=DELETE")
    _verify(target)
    target.chmod(0o600)
    with target.open("r+b") as file:
        os.fsync(file.fileno())


def _sync_directory(directory: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def backup(directory: Path, keep: int = 14, *, source: Path | None = None) -> Path:
    if keep < 1:
        raise ValueError("At least one backup must be retained")
    source = (source or database_path()).resolve()
    if not source.is_file():
        raise FileNotFoundError("Application database does not exist")
    directory = directory.expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"umytpa-{stamp}.sqlite3"
    descriptor, temporary = tempfile.mkstemp(prefix=".umytpa-backup-", dir=directory)
    os.close(descriptor)
    temp_path = Path(temporary)
    try:
        _copy_database(source, temp_path)
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)
    # Purge only our verified naming convention, never arbitrary files/symlinks.
    copies = sorted(p for p in directory.iterdir() if BACKUP_NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink())
    for obsolete in copies[:-keep]:
        obsolete.unlink()
    _sync_directory(directory)
    return destination


def restore(source: Path, *, services_stopped: bool, target: Path | None = None) -> Path | None:
    """Replace an offline DB only after integrity validation and a safety backup."""
    if not services_stopped:
        raise ValueError("Stop API and worker, then provide --services-stopped")
    source = source.expanduser().resolve()
    target = (target or database_path()).expanduser().resolve()
    if source == target:
        raise ValueError("Backup and application database must be different files")
    _verify(source)
    # Presence means a client may still be connected or uncheckpointed data exists.
    if any(Path(str(target) + suffix).exists() for suffix in ("-wal", "-shm")):
        raise ValueError("Database has WAL sidecars; close all clients and checkpoint before restore")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    previous = backup(target.parent / "before-restore", source=target) if target.exists() else None
    descriptor, temporary = tempfile.mkstemp(prefix=".umytpa-restore-", dir=target.parent)
    os.close(descriptor)
    temp_path = Path(temporary)
    try:
        _copy_database(source, temp_path)
        os.replace(temp_path, target)
        _sync_directory(target.parent)
    finally:
        temp_path.unlink(missing_ok=True)
    return previous


def backup_health(directory: Path, max_age: float = 26 * 3600) -> bool:
    if not directory.is_dir():
        return False
    copies = [p for p in directory.iterdir() if BACKUP_NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink()]
    return bool(copies) and time.time() - max(p.stat().st_mtime for p in copies) <= max_age


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup", "restore", "backup-loop", "backup-health"))
    parser.add_argument("--directory", type=Path, default=Path("/var/backups/umytpa"))
    parser.add_argument("--keep", type=int, default=14)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--services-stopped", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "restore":
            if not args.source:
                parser.error("restore requires --source")
            previous = restore(args.source, services_stopped=args.services_stopped)
            print(json.dumps({"event": "database_restored", "previous_backup": str(previous) if previous else None}))
        elif args.command == "backup-health":
            raise SystemExit(0 if backup_health(args.directory) else 1)
        elif args.command == "backup":
            print(json.dumps({"event": "database_backed_up", "file": str(backup(args.directory, args.keep))}))
        else:
            stopped = threading.Event()
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda _signal, _frame: stopped.set())
            while not stopped.is_set():
                backup(args.directory, args.keep)
                print(json.dumps({"event": "database_backed_up"}), flush=True)
                stopped.wait(86400)
    except (OSError, ValueError, sqlite3.Error, TimeoutError) as error:
        parser.exit(1, f"{type(error).__name__}: {error}\n")


if __name__ == "__main__":
    main()
