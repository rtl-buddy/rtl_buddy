"""Background detach for ``rb hub start --daemon`` (issue #387).

``--daemon`` used to be a stub: it printed "not implemented yet" and
then ran the server in the foreground, so the command blocked forever.
From a non-tty agent shell that reads as a hang, and because the
foreground loop deletes its own ``hub.json`` on shutdown, the discovery
record vanished again the moment the caller's timeout killed it — which
is why the bug report says hub.json was "never written".

Detaching by ``os.fork()`` alone was rejected. By the time
``rb hub start`` reaches this point the interpreter has imported Typer,
Rich, the whole ``rtl_buddy`` config stack and (on the ``--serve-viewer``
path) is about to probe ``importlib.metadata`` for the SPA bundle. A
fork without an exec hands the child a copy of that state including any
locks held by another thread, and on macOS a forked process that touches
the already-initialised Objective-C runtime is explicitly unsupported —
both classic sources of exactly the silent hang this issue reports.

So the daemon is spawned **fork + exec**: :func:`spawn_detached` runs a
fresh ``python -m rtl_buddy hub start --foreground ...`` in its own
session with stdio bound to ``hub.log``. That settles the ordering
question structurally — viewer-bundle discovery, ``--viewer-bundle``
resolution and every other import happen in the exec'd child, *after*
the detach, in exactly the code path ``--foreground`` already exercises.
The parent then waits for the child to publish ``hub.json`` so
``rb hub start --daemon`` only returns once the hub is really up.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from . import discovery


# How long the parent waits for the detached child to publish its
# discovery record before giving up. Generous: on a cold interpreter
# the child has to import the whole rtl_buddy stack plus (optionally)
# the viewer SPA package before it binds. Overridable for tests and for
# very slow network filesystems.
DEFAULT_READY_TIMEOUT_S = 30.0
READY_TIMEOUT_ENV = "RTL_BUDDY_HUB_DAEMON_TIMEOUT"

_POLL_INTERVAL_S = 0.05


class DaemonStartError(Exception):
    """The detached hub failed to come up.

    ``log_tail`` carries the last few lines the child wrote to
    ``hub.log`` so the CLI can show the real error (a bad ``--model``, a
    busy port) instead of a bare timeout.
    """

    def __init__(self, message: str, *, log_tail: str = "") -> None:
        super().__init__(message)
        self.log_tail = log_tail


def ready_timeout_s() -> float:
    """Seconds to wait for ``hub.json``; ``$RTL_BUDDY_HUB_DAEMON_TIMEOUT``."""
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

    Pure function so the flag round-trip is unit-testable without
    spawning anything. ``--foreground`` is always present and always
    first among the flags: the child must never re-enter this module,
    or a typo here becomes a spawn loop.

    ``python -m rtl_buddy`` rather than the ``rb`` console script so the
    daemon runs under the same interpreter as its parent even from a
    venv that isn't on ``PATH`` (same reasoning as
    :func:`rtl_buddy.hub.launchagent.render_plist`).
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
    """Start ``argv`` in its own session with stdio bound to ``log_path``.

    * ``start_new_session=True`` — the child leads a new session and has
      no controlling terminal, so it survives the parent shell exiting
      and never competes for the tty (or, under a non-tty agent shell,
      never inherits a pipe whose reader goes away).
    * stdin is ``/dev/null`` — a detached server that blocks on a read
      is the other half of this bug class.
    * stdout/stderr append to ``hub.log``, which is what the startup
      banner has always advertised and nothing previously wrote.
    """
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
        # The child holds its own dup of the fd; the parent is about to
        # exit and must not keep the log file open.
        log_handle.close()


def tail_log(log_path: Path, *, lines: int = 20) -> str:
    """Last ``lines`` of ``log_path``, or ``""`` when unreadable."""
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
    """Block until the detached child publishes its ``hub.json``.

    The record must carry the child's own PID: a stale file left behind
    by a crashed hub would otherwise be mistaken for a successful start.

    Raises :class:`DaemonStartError` when the child exits first (with the
    log tail attached, so the user sees the child's real error) or when
    the timeout expires.
    """
    # Resolved once: reading the env var again in the timeout message below
    # would report a number the deadline was not computed from.
    effective_timeout_s = timeout_s if timeout_s is not None else ready_timeout_s()
    deadline = time.monotonic() + effective_timeout_s
    while True:
        record = discovery.read_record(project_root)
        if record is not None and record.pid == proc.pid:
            return record

        exit_code = proc.poll()
        if exit_code is not None:
            # One last look: the child may have written the record and
            # exited between our read and this poll (a fast crash after
            # a successful bind still leaves a usable record behind, but
            # a genuine failure leaves nothing).
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
    """Spawn a detached hub and return its published discovery record.

    Convenience wrapper over :func:`build_daemon_argv`,
    :func:`spawn_detached` and :func:`wait_for_record` — the shape the
    CLI uses.
    """
    argv = build_daemon_argv(**argv_kwargs)  # type: ignore[arg-type]
    proc = spawn_detached(argv, project_root=project_root, log_path=log_path)
    try:
        return wait_for_record(
            project_root, proc=proc, log_path=log_path, timeout_s=timeout_s
        )
    except DaemonStartError:
        # Never leave a half-started daemon wedged in the background:
        # if it's still alive but hasn't published, it can't be found by
        # `rb hub stop` either.
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
