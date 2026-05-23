"""Background marimo launcher for the hub's "Open in marimo" endpoint.

Wraps ``rb axi-profile notebook --headless`` so the SPA can request a
deep-dive notebook over plain HTTP, get back a URL, and open it in a
new tab. The headless flow (rtl_buddy #190) means marimo doesn't
auto-pop a browser and doesn't require a session token — the SPA
opens the printed URL directly.

Why not just spawn-and-forget: marimo's edit server takes a few
seconds to bind. We need to wait until the URL is on stdout before
the hub responds to the SPA, otherwise the SPA opens a URL that
isn't ready yet (race on the websocket handshake) and the user
sees a "connection refused" pop-up. So we read stdout line-by-line
until either:

- the ``URL: http://...`` line lands → success, return the URL
- the subprocess exits → failure, surface its return code
- the watchdog timeout fires (default 30 s) → kill the subprocess
  and surface a timeout error

The spawned process is detached from the response cycle: once the
URL is captured, we return it and let marimo run until the user
closes the tab (or the hub shuts down). Marimo carries on serving
the notebook session in the background.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from ..logging_utils import log_event

logger = logging.getLogger(__name__)


# Marimo prints a line shaped like
#   ➜  URL: http://localhost:2719
# (no token query string when --no-token is set). The exact prefix
# has shifted across marimo versions; the regex picks any "URL: …"
# substring as long as it points at http(s).
_URL_LINE_RE = re.compile(rb"URL:\s*(https?://\S+)")

DEFAULT_TIMEOUT_S = 30.0


class AxiNotebookLaunchError(RuntimeError):
    """Surfaced as an HTTP 4xx/5xx by the route handler."""

    def __init__(self, message: str, *, status: int = 500):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class LaunchResult:
    """What the SPA needs to open the notebook."""

    url: str
    pid: int
    port: int
    test: str
    suite_dir: str


def _find_free_port() -> int:
    """Bind a transient socket to an OS-assigned port, close it, and
    use the number. Race-prone in theory (another process could grab
    the port between close and the marimo bind) but cheap in
    practice for single-user local-loopback flows."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _validate_suite_dir(suite_dir: str, project_root: Path) -> Path:
    """Resolve + validate ``suite_dir`` is real, contains a
    ``tests.yaml``, and lives under ``project_root`` (path-traversal
    guard so the SPA can't make us spawn against an arbitrary FS
    location).
    """
    if not suite_dir:
        raise AxiNotebookLaunchError("suite_dir is required", status=400)
    candidate = Path(suite_dir)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, FileNotFoundError):
        raise AxiNotebookLaunchError(
            f"suite_dir does not exist: {suite_dir}", status=400
        ) from None
    if not resolved.is_dir():
        raise AxiNotebookLaunchError(
            f"suite_dir is not a directory: {suite_dir}", status=400
        )
    project_resolved = project_root.resolve()
    try:
        resolved.relative_to(project_resolved)
    except ValueError:
        raise AxiNotebookLaunchError(
            "suite_dir must be under the hub's project_root", status=400
        ) from None
    if not (resolved / "tests.yaml").is_file():
        raise AxiNotebookLaunchError(
            f"suite_dir has no tests.yaml: {resolved}", status=400
        )
    return resolved


def _validate_test_name(test: str) -> str:
    """Allow the same name shape ``tests.yaml`` does — alphanum + the
    handful of separators we've seen in the wild. Rejects shell-ish
    or path-ish chars so we can't be tricked into emitting
    ``rb axi-profile notebook 'foo; rm -rf /'``."""
    if not test:
        raise AxiNotebookLaunchError("test is required", status=400)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", test):
        raise AxiNotebookLaunchError(
            f"test name has unexpected characters: {test!r}", status=400
        )
    return test


def _resolve_rb_executable() -> str:
    """``rb`` (the rtl_buddy CLI) is the entry point. Prefer the same
    interpreter we're running under (``sys.executable -m rtl_buddy``)
    so the spawned subprocess sees the same venv as the hub —
    matters when the rtl_buddy install is editable and the system
    PATH points at a different copy.
    """
    return sys.executable


def _build_cmd(
    *,
    suite_dir: Path,
    test: str,
    port: int,
) -> list[str]:
    """The subprocess we'll spawn — equivalent to
    ``cd <suite_dir> && rb axi-profile notebook <test> --headless --port N -c tests.yaml``
    but invoked via ``-m rtl_buddy`` against the hub's interpreter.
    """
    return [
        _resolve_rb_executable(),
        "-m",
        "rtl_buddy",
        "axi-profile",
        "notebook",
        test,
        "-c",
        "tests.yaml",
        "--headless",
        "--port",
        str(port),
    ]


async def _wait_for_url(
    proc: asyncio.subprocess.Process,
    *,
    timeout_s: float,
) -> str:
    """Read ``proc.stdout`` line-by-line until the URL line lands or
    we time out. Returns the URL string (decoded).

    Marimo can emit a few informational lines first (update banner,
    edit hint) so we keep reading until we either hit the URL or
    decide it's not coming.
    """
    assert proc.stdout is not None
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise AxiNotebookLaunchError(
                f"marimo did not print a URL within {timeout_s:.0f}s",
                status=504,
            )
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            raise AxiNotebookLaunchError(
                f"marimo did not print a URL within {timeout_s:.0f}s",
                status=504,
            ) from None
        if not line:
            # EOF before URL → subprocess exited unexpectedly.
            rc = await proc.wait()
            raise AxiNotebookLaunchError(
                f"marimo exited with code {rc} before printing a URL",
                status=500,
            )
        m = _URL_LINE_RE.search(line)
        if m:
            return m.group(1).decode("utf-8", errors="replace")


async def launch(
    *,
    test: str,
    suite_dir: str,
    project_root: Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> LaunchResult:
    """Validate inputs, spawn marimo headless, return the URL.

    Raises :class:`AxiNotebookLaunchError` on any failure — the route
    handler maps ``.status`` to the HTTP response code.
    """
    test = _validate_test_name(test)
    resolved_suite = _validate_suite_dir(suite_dir, project_root)

    # Quick env sanity-check up front so the user gets a clear
    # message before we go through subprocess gymnastics.
    if shutil.which("marimo") is None:
        raise AxiNotebookLaunchError(
            "marimo not on PATH; install rtl-buddy-axi-profiler with the "
            "[notebook] extra so the hub can spawn it.",
            status=503,
        )

    port = _find_free_port()
    cmd = _build_cmd(suite_dir=resolved_suite, test=test, port=port)

    log_event(
        logger,
        logging.INFO,
        "hub.axi_notebook.launching",
        test=test,
        suite_dir=str(resolved_suite),
        port=port,
        cmd=" ".join(cmd),
    )

    # PYTHONUNBUFFERED=1 forces line-buffered stdout so we see
    # marimo's URL line as soon as it's printed (rather than after
    # the kernel's block-buffer fills).
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(resolved_suite),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        # New process group so a hub SIGTERM doesn't immediately
        # kill marimo — the user's notebook session is supposed to
        # outlive a single hub restart in the worst case.
        start_new_session=True,
    )

    try:
        url = await _wait_for_url(proc, timeout_s=timeout_s)
    except AxiNotebookLaunchError:
        # Kill the partially-started subprocess so we don't leak it
        # on every failed launch attempt.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise

    assert proc.pid is not None
    log_event(
        logger,
        logging.INFO,
        "hub.axi_notebook.launched",
        test=test,
        url=url,
        pid=proc.pid,
        port=port,
    )
    return LaunchResult(
        url=url,
        pid=proc.pid,
        port=port,
        test=test,
        suite_dir=str(resolved_suite),
    )
