"""Find and run the external tools: ffmpeg, ffprobe and the Gyroflow CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path


class ToolError(RuntimeError):
    pass


class Cancelled(ToolError):
    pass


# Cancel kills whatever is running, from any thread. The worker also checks
# the flag before starting the next process.
_cancel = threading.Event()
_running: set[subprocess.Popen] = set()
_lock = threading.Lock()


def cancel() -> None:
    _cancel.set()
    with _lock:
        for p in _running:
            p.kill()


def reset_cancel() -> None:
    _cancel.clear()


def check_cancel() -> None:
    if _cancel.is_set():
        raise Cancelled("cancelled")


def _candidates_gyroflow() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        return [Path("/Applications/Gyroflow.app/Contents/MacOS/gyroflow"),
                home / "Applications/Gyroflow.app/Contents/MacOS/gyroflow"]
    if sys.platform == "win32":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("LOCALAPPDATA", str(home / "AppData/Local"))]
        return [Path(r) / "Gyroflow" / "gyroflow.exe" for r in roots]
    return [Path("/usr/bin/gyroflow"), Path("/opt/Gyroflow/gyroflow"), home / "Gyroflow/gyroflow"]


def find_gyroflow(configured: str | None = None) -> Path:
    if configured:
        p = Path(configured)
        if not p.exists():
            raise ToolError(f"Gyroflow not found at {p}")
        return _check_gyroflow(p)
    on_path = shutil.which("gyroflow")
    for p in ([Path(on_path)] if on_path else []) + _candidates_gyroflow():
        if p.exists():
            return _check_gyroflow(p)
    raise ToolError("Gyroflow not found. Set its path in settings.")


def _check_gyroflow(exe: Path) -> Path:
    # The Mac App Store build is sandboxed: its CLI cannot read files it was not
    # handed through a file dialog, so every clip fails to load.
    for parent in exe.parents:
        if parent.suffix == ".app":
            if (parent / "Contents/_MASReceipt").exists():
                raise ToolError("This Gyroflow is the Mac App Store build, whose command line cannot read "
                                "clips. Install the build from github.com/gyroflow/gyroflow/releases.")
            break
    return exe


def find(name: str, configured: str | None = None) -> Path:
    if configured:
        return Path(configured)
    found = shutil.which(name)
    if found:
        return Path(found)
    # GUI apps on macOS do not inherit the shell's PATH.
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        if (Path(d) / name).exists():
            return Path(d) / name
    raise ToolError(f"{name} not found. Install ffmpeg (8.0 or newer for ProRes RAW) or set its path.")


def run(cmd: list, timeout: float | None = None) -> subprocess.CompletedProcess:
    check_cancel()
    env = dict(os.environ, LANG="en_US.UTF-8")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    args = [str(c) for c in cmd]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         env=env, creationflags=flags)
    with _lock:
        _running.add(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise ToolError(f"{Path(args[0]).name} did not finish in {timeout:.0f} s and was stopped") from None
    finally:
        with _lock:
            _running.discard(p)
    check_cancel()
    return subprocess.CompletedProcess(args, p.returncode, out, err)
