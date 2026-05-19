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

from ..logging_utils import log_event
from .config import HubConfig
from .discovery import delete_record_if_owner, write_record
from .server import HubServer


logger = logging.getLogger(__name__)


def _server_version() -> str:
    try:
        return _pkg_version("rtl-buddy")
    except PackageNotFoundError:
        return "0.0.0+unknown"


async def _run(project_root: Path, config: HubConfig) -> int:
    server = HubServer(
        host="127.0.0.1",
        port=config.hub.listen_port,
        server_version=_server_version(),
    )
    host, port = await server.start()

    write_record(
        project_root,
        pid=os.getpid(),
        tcp=f"{host}:{port}",
        server_version=server.server_version,
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
    stop_task = asyncio.create_task(stop_event.wait(), name="hub-stop")

    try:
        done, _pending = await asyncio.wait(
            {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc
    finally:
        await server.shutdown()
        serve_task.cancel()
        try:
            await serve_task
        except (asyncio.CancelledError, Exception):
            pass
        stop_task.cancel()
        delete_record_if_owner(project_root, expected_pid=os.getpid())

    return 0


def serve(project_root: Path, config: HubConfig) -> int:
    """Run the hub event loop until exit. Returns the process exit code."""

    return asyncio.run(_run(project_root, config))


__all__ = ["serve"]
