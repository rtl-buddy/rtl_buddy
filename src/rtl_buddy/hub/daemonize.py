"""Detach ``rb hub start --daemon`` into a background process.

The daemon is a fresh ``python -m rtl_buddy hub start --foreground`` in its own session with stdio bound to ``hub.log``. The parent waits for the child to publish ``hub.json``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from . import discovery


DEFAULT_READY_TIMEOUT_S = 30.0
READY_TIMEOUT_ENV = "RTL_BUDDY_HUB_DAEMON_TIMEOUT"

_POLL_INTERVAL_S = 0.05


class DaemonStartError(Exception):
    """The detached hub failed to start. ``log_tail`` holds the last lines of ``hub.log``."""

    def __init__(self, message: str, *, log_tail: str = "") -> None:
        super().__init__(message)
        self.log_tail = log_tail


def ready_timeout_s() -> float:
    """Return the seconds to wait for ``hub.json``, from ``$RTL_BUDDY_HUB_DAEMON_TIMEOUT`` or the default."""
    raw = os.environ.get(READY_TIMEOUT_ENV)
    if not raw:
        return DEFAULT_READY_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_READY_TIMEOUT_S
    return value if value > 0 else DEFAULT_READY_TIMEOUT_S


def build_daemon_argv(
    *,
    serve_viewer: bool = False,
    viewer_bundle: Path | None = None,
    listen_port: int | None = None,
    http_port: int | None = None,
    model: str | None = None,
    models_file: Path | None = None,
    axi_perf_from: Path | None = None,
    python: str | None = None,
) -> list[str]:
    """Build the child command line for a detached ``rb hub start``.

    ``--foreground`` is always passed so the child never re-enters the daemon path. It runs ``python -m rtl_buddy`` under the parent's interpreter.
    """
    argv = [
        python or sys.executable,
        "-m",
        "rtl_buddy",
        "hub",
        "start",
        "--foreground",
    ]
    if serve_viewer:
        argv.append("--serve-viewer")
    if viewer_bundle is not None:
        argv += ["--viewer-bundle", str(viewer_bundle)]
    if listen_port is not None:
        argv += ["--listen-port", str(listen_port)]
    if http_port is not None:
        argv += ["--http-port", str(http_port)]
    if model is not None:
        argv += ["--model", model]
    if models_file is not None:
        argv += ["--models-file", str(models_file)]
    if axi_perf_from is not None:
        argv += ["--axi-perf-from", str(axi_perf_from)]
    return argv


def spawn_detached(
    argv: list[str],
    *,
    project_root: Path,
    log_path: Path,
) -> subprocess.Popen[bytes]:
    """Start ``argv`` in a new session with stdin from ``/dev/null`` and stdout/stderr appended to ``log_path``."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab")
    try:
        return subprocess.Popen(
            argv,
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        # The child has its own copy of the fd.
        log_handle.close()


def tail_log(log_path: Path, *, lines: int = 20) -> str:
    """Return the last ``lines`` lines of ``log_path``, or ``""`` when unreadable."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def wait_for_record(
    project_root: Path,
    *,
    proc: subprocess.Popen[bytes],
    log_path: Path,
    timeout_s: float | None = None,
) -> discovery.HubRecord:
    """Block until the child publishes a ``hub.json`` carrying its own PID.

    Raises :class:`DaemonStartError`, with the log tail attached, when the child exits first or the timeout expires.
    """
    effective_timeout_s = timeout_s if timeout_s is not None else ready_timeout_s()
    deadline = time.monotonic() + effective_timeout_s
    while True:
        record = discovery.read_record(project_root)
        if record is not None and record.pid == proc.pid:
            return record

        exit_code = proc.poll()
        if exit_code is not None:
            # The child may have written the record between the read and the poll.
            record = discovery.read_record(project_root)
            if record is not None and record.pid == proc.pid:
                return record
            raise DaemonStartError(
                f"the detached hub (pid {proc.pid}) exited with code "
                f"{exit_code} before writing "
                f"{discovery.discovery_path(project_root)}.",
                log_tail=tail_log(log_path),
            )

        if time.monotonic() >= deadline:
            raise DaemonStartError(
                f"timed out after {effective_timeout_s:.0f}s "
                f"waiting for the detached hub (pid {proc.pid}) to write "
                f"{discovery.discovery_path(project_root)}.",
                log_tail=tail_log(log_path),
            )
        time.sleep(_POLL_INTERVAL_S)


def start_detached(
    project_root: Path,
    *,
    log_path: Path,
    timeout_s: float | None = None,
    **argv_kwargs: object,
) -> discovery.HubRecord:
    """Spawn a detached hub and return its discovery record."""
    argv = build_daemon_argv(**argv_kwargs)  # type: ignore[arg-type]
    proc = spawn_detached(argv, project_root=project_root, log_path=log_path)
    try:
        return wait_for_record(
            project_root, proc=proc, log_path=log_path, timeout_s=timeout_s
        )
    except DaemonStartError:
        # A live child without a record cannot be found by `rb hub stop`.
        if proc.poll() is None:
            proc.terminate()
        raise


__all__ = [
    "DaemonStartError",
    "DEFAULT_READY_TIMEOUT_S",
    "READY_TIMEOUT_ENV",
    "build_daemon_argv",
    "ready_timeout_s",
    "spawn_detached",
    "start_detached",
    "tail_log",
    "wait_for_record",
]
