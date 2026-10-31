"""Small OS helpers: background priority, keeping the Mac awake, logging,
notifications."""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import subprocess
import sys
from pathlib import Path

from handy_worker.conditions import CAFFEINATE

log = logging.getLogger(__name__)

OSASCRIPT = "/usr/bin/osascript"


def lower_priority() -> None:
    """Background priority even outside launchd (Run now, idle trigger):
    nice 10, plus macOS background QoS for CPU and I/O via taskpolicy."""
    with contextlib.suppress(OSError):
        os.nice(10)
    if sys.platform == "darwin":
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["/usr/sbin/taskpolicy", "-b", "-p", str(os.getpid())],
                capture_output=True,
                timeout=5,
                check=False,
            )


class Caffeinate:
    """``caffeinate -i -w <pid>``: no idle sleep while this process runs.
    Only started on mains power; ends by itself if the worker dies."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> bool:
        if sys.platform != "darwin" or not Path(CAFFEINATE).exists():
            return False
        try:
            self.proc = subprocess.Popen(
                [CAFFEINATE, "-i", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            log.warning("caffeinate failed: %s", e)
            return False
        return True

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None


def setup_logging(log_file: Path, verbose: bool = False, to_stderr: bool = True) -> None:
    """Rotating log in ``~/Library/Logs/<app name>/`` (5 × 2 MB)."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(process)d %(name)s: %(message)s")
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        print(f"warning: cannot write logs to {log_file}: {e}", file=sys.stderr)
    if to_stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def applescript_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, body: str, app_name: str) -> None:
    """A macOS notification (best effort). Only used when the service needs
    attention; never on success."""
    if sys.platform != "darwin":
        log.info("notification: %s — %s", title, body)
        return
    script = (
        f"display notification {applescript_string(body)} "
        f"with title {applescript_string(f'{app_name}: {title}')}"
    )
    try:
        subprocess.run([OSASCRIPT, "-e", script], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("notification failed: %s", e)
