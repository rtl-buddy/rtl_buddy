"""asyncio orchestration for ``rb hub start``.

Starts the hub server and viewer listeners, writes the discovery record, and runs the event loop until a signal, ``rb hub stop`` or Ctrl-C.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import signal
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from ..errors import FatalRtlBuddyError
from ..logging_utils import emit_console_text, log_event
from ..tool_manifest import viewer_dist_version
from .config import HubConfig
from . import landing_page
from .discovery import delete_record_if_owner, write_record
from .resolver import Resolver, default_view_json_path
from .server import HubServer
from .viewer_http import ViewerServer


logger = logging.getLogger(__name__)


# Minimum rtl-buddy-sch / rtl-buddy-view release for the in-env SPA bundle.
# Keep equal to the floor in tool_manifest.py. rtl_buddy declares no pin in
# pyproject.toml, so this runtime check is the only floor for the in-process bundle.
_VIEW_MIN_VERSION = "0.3.0"


def _version_tuple(version: str) -> tuple[int, ...]:
    """Leading (major, minor, patch) ints of a version string; suffixes are dropped."""
    parts = []
    for segment in version.split(".")[:3]:
        match = re.match(r"\d+", segment)
        parts.append(int(match.group()) if match else 0)
    return tuple(parts)


def _check_view_version() -> None:
    """Fail fast when the in-env viewer is older than the floor.

    Runs when the in-process bundle is consumed, so an old editable or git
    install gives a hint instead of a stale SPA. Skipped when the installed
    version cannot be read. Both dist names are probed (``viewer_dist_version()``).
    """
    found = viewer_dist_version()
    if found is None:
        return
    dist, installed = found
    if _version_tuple(installed) < _version_tuple(_VIEW_MIN_VERSION):
        raise FatalRtlBuddyError(
            f"rb hub --serve-viewer requires rtl-buddy-sch >= "
            f"{_VIEW_MIN_VERSION}, but {dist} {installed} is installed. "
            f"rtl-buddy-sch is the renamed rtl-buddy-view dist, so pip "
            f"cannot upgrade one into the other — remove the old one "
            f"first or both ship the same files:\n"
            f"    pip uninstall -y rtl-buddy-view && "
            f'pip install -U "rtl-buddy-sch >= {_VIEW_MIN_VERSION}"'
        )


class _PortInUseError(Exception):
    """Bind failed because the port is held by another process.

    ``serve`` turns it into a one-line error and exit code 1.
    """

    def __init__(self, role: str, port: int) -> None:
        self.role = role
        self.port = port
        super().__init__(f"{role} port {port} already in use")


async def _start_listener(coro, *, role: str, port: int):
    """Run a listener-bind coroutine, translating EADDRINUSE into :class:`_PortInUseError`."""
    try:
        return await coro
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise _PortInUseError(role=role, port=port) from exc
        raise


def _server_version() -> str:
    try:
        return _pkg_version("rtl-buddy")
    except PackageNotFoundError:
        return "0.0.0+unknown"


def _print_startup_banner(
    *,
    tcp_host: str,
    tcp_port: int,
    http_port: int | None,
    view_json_path: Path | None,
    log_path: Path | None,
) -> None:
    """Print connection info so the user is not left guessing after ``rb hub start`` blocks.

    Adapter peers find the hub through ``.rtl-buddy/hub.json``; the banner is
    for the person at the terminal. ``--daemon`` prints its own, shorter banner.
    """
    lines = ["rtl-buddy-hub running."]
    if http_port is not None:
        base = f"http://127.0.0.1:{http_port}"
        # The landing page is the URL to hand a person; the SPA keeps its
        # own line for scripts and bookmarks.
        lines.append(f"  Hub:      {base}/")
        url = f"{base}{landing_page.VIEW_PAGE_ROUTE}"
        # Auto-load view.json only when it exists, or the SPA opens empty.
        if view_json_path is not None and view_json_path.is_file():
            url += "?view=/view.json"
        lines.append(f"  Viewer:   {url}")
    lines.append(f"  TCP:      {tcp_host}:{tcp_port}")
    if log_path is not None:
        lines.append(f"  Logs:     {log_path}")
    lines.append("Press Ctrl-C to stop.")
    emit_console_text("\n".join(lines))


def _discover_viewer_bundle() -> Path | None:
    """Return the SPA bundle shipped by rtl-buddy-view, or ``None``.

    Lets ``--serve-viewer`` work without ``--viewer-bundle``. The package is an
    optional peer, so a missing import means no bundle.
    """
    try:
        from rtl_buddy_view import viewer_bundle  # type: ignore[import-not-found]
    except ImportError:
        return None
    # Raises FatalRtlBuddyError when the installed viewer is below the floor.
    _check_view_version()
    try:
        return viewer_bundle.path()
    except Exception:  # noqa: BLE001 - defensive against API drift in the peer package
        return None


async def _run(
    project_root: Path,
    config: HubConfig,
    *,
    serve_viewer: bool = False,
    viewer_bundle: Path | None = None,
    view_json_override: Path | None = None,
    initial_model: str | None = None,
    models_file_pin: Path | None = None,
    axi_perf_source: Path | None = None,
) -> int:
    if view_json_override is not None:
        # --model has already generated view.json; it overrides [mapping].view_json.
        view_json_path = view_json_override
    elif config.mapping.view_json:
        view_json_path = (project_root / config.mapping.view_json).resolve()
    else:
        view_json_path = default_view_json_path(project_root)
    resolver = Resolver(view_json_path=view_json_path, mapping=config.mapping)

    server = HubServer(
        host="127.0.0.1",
        port=config.hub.listen_port,
        server_version=_server_version(),
        resolver=resolver,
    )
    host, port = await _start_listener(
        server.start(), role="TCP", port=config.hub.listen_port
    )

    viewer: ViewerServer | None = None
    http_port: int | None = None
    if serve_viewer:
        resolved_bundle = viewer_bundle
        if resolved_bundle is None:
            resolved_bundle = _discover_viewer_bundle()
            if resolved_bundle is not None:
                log_event(
                    logger,
                    logging.INFO,
                    "hub.viewer.bundle_auto_discovered",
                    path=str(resolved_bundle),
                )
        viewer = ViewerServer(
            hub_host=host,
            hub_port=port,
            http_port=config.hub.http_port,
            viewer_bundle=resolved_bundle,
            view_json_path=view_json_path,
            project_root=project_root,
            initial_model=initial_model,
            models_file_pin=models_file_pin,
            axi_perf_source=axi_perf_source,
            hub_server=server,
        )
        _vhost, vport = await _start_listener(
            viewer.start(), role="HTTP", port=config.hub.http_port
        )
        http_port = vport
        log_event(
            logger,
            logging.INFO,
            "hub.viewer.url",
            url=f"http://127.0.0.1:{vport}/",
        )

    write_record(
        project_root,
        pid=os.getpid(),
        tcp=f"{host}:{port}",
        server_version=server.server_version,
        http_port=http_port,
        active_model=initial_model,
    )

    _print_startup_banner(
        tcp_host=host,
        tcp_port=port,
        http_port=http_port,
        view_json_path=view_json_path,
        log_path=(project_root / config.hub.log_path).resolve()
        if config.hub.log_path
        else None,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop(signame: str) -> None:
        log_event(logger, logging.INFO, "hub.signal", name=signame)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop, sig.name)
        except NotImplementedError:
            # add_signal_handler is unsupported on Windows and some embedded loops.
            pass

    serve_task = asyncio.create_task(server.serve_forever(), name="hub-serve")
    viewer_task: asyncio.Task[None] | None = None
    if viewer is not None:
        viewer_task = asyncio.create_task(
            viewer.serve_forever(), name="hub-viewer-http"
        )
    stop_task = asyncio.create_task(stop_event.wait(), name="hub-stop")

    watched: set[asyncio.Task[Any]] = {serve_task, stop_task}
    if viewer_task is not None:
        watched.add(viewer_task)

    try:
        done, _pending = await asyncio.wait(
            watched, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc
    finally:
        if viewer is not None:
            await viewer.shutdown()
        await server.shutdown()
        for task in (serve_task, viewer_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        stop_task.cancel()
        delete_record_if_owner(project_root, expected_pid=os.getpid())

    return 0


def serve(
    project_root: Path,
    config: HubConfig,
    *,
    serve_viewer: bool = False,
    viewer_bundle: Path | None = None,
    view_json_override: Path | None = None,
    initial_model: str | None = None,
    models_file_pin: Path | None = None,
    axi_perf_source: Path | None = None,
) -> int:
    """Run the hub event loop until exit; return the process exit code.

    - ``view_json_override``: overrides ``[mapping].view_json`` from hub.toml
      (``rb hub start --model NAME``).
    - ``initial_model``: the start-time ``--model`` selection, reported by
      ``GET /models`` and ``.rtl-buddy/hub.json``.
    - ``models_file_pin``: ``--models-file PATH``; ``GET /models`` and
      ``GET /view.json?model=`` refuse model names not in that file.
    - ``axi_perf_source``: ``--axi-perf-from PATH``; forwarded to every
      ``build_view_json`` call so view.json carries the axi-perf overlay and
      source metadata.
    """

    try:
        return asyncio.run(
            _run(
                project_root,
                config,
                serve_viewer=serve_viewer,
                viewer_bundle=viewer_bundle,
                view_json_override=view_json_override,
                initial_model=initial_model,
                models_file_pin=models_file_pin,
                axi_perf_source=axi_perf_source,
            )
        )
    except _PortInUseError as exc:
        # Rich parses `[hub]` as a style tag; `\[` escapes it so the hub.toml
        # section name renders.
        which_toml = (
            r"\[hub].http_port" if exc.role == "HTTP" else r"\[hub].listen_port"
        )
        which_flag = "--http-port" if exc.role == "HTTP" else "--listen-port"
        emit_console_text(
            f"rb hub start: {exc.role} port {exc.port} already in use. "
            f"Pick another port in {which_toml} (hub.toml) or "
            f"{which_flag} N, or stop the process holding it.",
            style="red",
        )
        log_event(
            logger,
            logging.ERROR,
            "hub.bind.port_in_use",
            role=exc.role,
            port=exc.port,
        )
        return 1


__all__ = ["serve"]
