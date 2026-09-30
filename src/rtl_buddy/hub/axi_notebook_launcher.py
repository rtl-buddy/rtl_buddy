"""Spawn ``rb axi-profile notebook --headless`` for the hub's "Open in marimo" endpoint and return the notebook URL.

The launcher waits for marimo to print its URL before returning, so the SPA never opens a URL that is not yet serving. The marimo process keeps running after the URL is returned.
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


# Matches marimo's "URL: http://..." line; the prefix varies across marimo versions.
_URL_LINE_RE = re.compile(rb"URL:\s*(https?://\S+)")

DEFAULT_TIMEOUT_S = 30.0


class AxiNotebookLaunchError(RuntimeError):
    """Launch failure; ``status`` is the HTTP code the route handler returns."""

    def __init__(self, message: str, *, status: int = 500):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class LaunchResult:
    """Notebook URL and process details returned to the SPA."""

    url: str
    pid: int
    port: int
    test: str
    suite_dir: str


def _find_free_port() -> int:
    """Return a free loopback port. Another process could take it before marimo binds."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _validate_suite_dir(suite_dir: str, project_root: Path) -> Path:
    """Resolve ``suite_dir`` and require an existing directory under ``project_root`` that contains ``tests.yaml``."""
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
    """Accept only letters, digits, ``_``, ``.`` and ``-`` in the test name."""
    if not test:
        raise AxiNotebookLaunchError("test is required", status=400)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", test):
        raise AxiNotebookLaunchError(
            f"test name has unexpected characters: {test!r}", status=400
        )
    return test


def _resolve_rb_executable() -> str:
    """Return the hub's own interpreter so the subprocess uses the same rtl_buddy install."""
    return sys.executable


def _build_cmd(
    *,
    suite_dir: Path,
    test: str,
    port: int,
) -> list[str]:
    """Return the argv for ``rb axi-profile notebook <test> --headless --port N -c tests.yaml``."""
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
    """Read ``proc.stdout`` until the URL line appears and return the URL.

    Raises :class:`AxiNotebookLaunchError` on timeout (504) or early exit (500).
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
    events_url: str | None = None,
) -> LaunchResult:
    """Validate inputs, spawn marimo headless and return its URL.

    Raises :class:`AxiNotebookLaunchError` on any failure.
    """
    test = _validate_test_name(test)
    resolved_suite = _validate_suite_dir(suite_dir, project_root)

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

    # Unbuffered stdout, so the URL line is seen as soon as it is printed.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if events_url:
        # The notebook's sync module joins the hub event broker at this URL.
        env["RB_HUB_EVENTS_URL"] = events_url
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(resolved_suite),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        # New session so a hub SIGTERM does not kill marimo.
        start_new_session=True,
    )

    try:
        url = await _wait_for_url(proc, timeout_s=timeout_s)
    except AxiNotebookLaunchError:
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
