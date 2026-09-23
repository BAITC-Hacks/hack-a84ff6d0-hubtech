"""Stop only subprocess trees launched and owned by the current test."""
import os
import subprocess


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def terminate_owned_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        # Some Windows Python distributions launch an interpreter child. Stopping
        # the wrapper alone leaves that child holding SQLite files and sockets.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10, check=False, creationflags=NO_WINDOW)
    else:
        process.terminate()
