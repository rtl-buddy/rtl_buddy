"""asyncio orchestration for ``rb hub start``.

This module glues the server (:mod:`rtl_buddy.hub.server`) to the
discovery and config layers and runs the event loop until a signal,
``rb hub stop``, or Ctrl-C asks the daemon to exit.

Kept narrow: anything specific to clients (the resolver, WCP bridge,
viewer HTTP layer) lives in its own module so this file stays an
obviously-correct boot sequence.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from ..logging_utils import emit_console_text, log_event
from .config import HubConfig
from .discovery import delete_record_if_owner, write_record
from .resolver import Resolver, default_view_json_path
from .server import HubServer
from .viewer_http import ViewerServer


logger = logging.getLogger(__name__)


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
    """Print connection info to stdout so the user isn't left guessing
    after ``rb hub start`` blocks the terminal.

    Adapter peers (nvim, ``rb wave``) auto-discover the hub via
    ``.rtl-buddy/hub.json`` so they don't need this output — the
    browser-bound viewer URL is the main thing we're surfacing. The
    explicit "Press Ctrl-C" line documents that the foregrounded
    process is by design (``--daemon`` warns and stays in foreground).
    """
    lines = ["rtl-buddy-hub running."]
    if http_port is not None:
        url = f"http://127.0.0.1:{http_port}/"
        # Append the auto-load query string only when the view.json is
        # actually servable — otherwise it'd 404 and the SPA would land
        # in the empty state with a misleading URL on the user's first
        # click.
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

    Lets ``rb hub start --serve-viewer`` work without ``--viewer-bundle``
    when the user has rtl-buddy-view installed alongside rtl-buddy. The
    package is an optional runtime peer — the hub doesn't declare it as
    a hard dep, so the import is wrapped and a missing module is just
    "no bundle here, use the placeholder."
    """
    try:
        from rtl_buddy_view import viewer_bundle  # type: ignore[import-not-found]
    except ImportError:
        return None
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
) -> int:
    if config.mapping.view_json:
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
    host, port = await server.start()

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
        )
        _vhost, vport = await viewer.start()
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
            # Windows or certain embedded loops don't support add_signal_handler.
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
) -> int:
    """Run the hub event loop until exit. Returns the process exit code."""

    return asyncio.run(
        _run(
            project_root,
            config,
            serve_viewer=serve_viewer,
            viewer_bundle=viewer_bundle,
        )
    )


__all__ = ["serve"]
