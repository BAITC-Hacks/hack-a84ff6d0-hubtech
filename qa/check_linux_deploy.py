"""Check shipped nginx and systemd syntax without starting or changing services.

Run on Linux with nginx, openssl and systemd-analyze installed.
Temporary certificates and adjusted validation-only paths are deleted afterward.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    required = ("nginx", "openssl", "systemd-analyze")
    commands = {name: shutil.which(name) for name in required}
    if any(not command for command in commands.values()):
        raise SystemExit("Install nginx, openssl and systemd-analyze on Linux before this check")
    with tempfile.TemporaryDirectory(prefix="umytpa-deploy-check-") as temporary:
        directory = Path(temporary)
        subprocess.run([
            commands["openssl"], "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-keyout", str(directory / "privkey.pem"),
            "-out", str(directory / "fullchain.pem"),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        site = (ROOT / "deploy/nginx.conf").read_text(encoding="utf-8")
        site = site.replace("/etc/umytpa/tls", str(directory))
        site = site.replace("/opt/umytpa/frontend/dist", str(ROOT / "frontend/dist"))
        (directory / "site.conf").write_text(site, encoding="utf-8")
        (directory / "nginx.conf").write_text(
            f"pid {directory}/nginx.pid;\nerror_log stderr;\nevents {{}}\n"
            f"http {{ include /etc/nginx/mime.types; include {directory}/site.conf; }}\n", encoding="utf-8")
        subprocess.run([commands["nginx"], "-t", "-p", str(directory), "-c", str(directory / "nginx.conf")], check=True)
        units = []
        for source in sorted((ROOT / "deploy").glob("umytpa-*")):
            if source.suffix not in {".service", ".timer"}:
                continue
            content = source.read_text(encoding="utf-8")
            content = content.replace("/opt/umytpa/backend/.venv/bin/python", sys.executable)
            content = content.replace("/opt/umytpa/backend", str(ROOT / "backend"))
            target = directory / source.name
            target.write_text(content, encoding="utf-8")
            units.append(str(target))
        subprocess.run([commands["systemd-analyze"], "verify", *units], check=True)
    print("nginx and all systemd units passed syntax validation; no services were started")


if __name__ == "__main__":
    main()
