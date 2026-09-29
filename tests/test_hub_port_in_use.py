"""Tests for the clean error when ``rb hub start`` finds its port already in use."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from pathlib import Path

import pytest

from rtl_buddy.hub.config import HubConfig, HubMappingConfig, HubServerConfig
from rtl_buddy.hub.loop import _PortInUseError, _start_listener, serve


@contextlib.contextmanager
def _hold_port(port: int):
    """Bind a listener on (127.0.0.1, port) and yield the port."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    s.bind(("127.0.0.1", port))
    s.listen()
    try:
        yield s.getsockname()[1]
    finally:
        s.close()


@pytest.mark.asyncio
async def test_start_listener_translates_eaddrinuse_to_port_in_use():
    """The helper wrapping server and viewer ``start()`` reports a bind conflict."""

    with _hold_port(0) as taken:

        async def bind_again():
            srv = await asyncio.start_server(
                lambda r, w: None, host="127.0.0.1", port=taken
            )
            return srv

        with pytest.raises(_PortInUseError) as info:
            await _start_listener(bind_again(), role="TCP", port=taken)
        assert info.value.role == "TCP"
        assert info.value.port == taken


@pytest.mark.asyncio
async def test_start_listener_passes_other_oserror_through():
    """OSErrors other than EADDRINUSE propagate unchanged."""

    async def boom():
        raise OSError(13, "permission denied")

    with pytest.raises(OSError) as info:
        await _start_listener(boom(), role="TCP", port=42)
    assert info.value.errno == 13


def test_serve_returns_1_and_emits_clean_message_on_eaddrinuse(tmp_path: Path, capfd):
    """``serve()`` with an occupied pinned ``listen_port`` returns exit code 1 and a
    one-line message without a traceback.
    """

    with _hold_port(0) as taken:
        cfg = HubConfig(
            hub=HubServerConfig(listen_port=taken, http_port=0),
            mapping=HubMappingConfig(),
        )
        rc = serve(tmp_path, cfg, serve_viewer=False)

    assert rc == 1
    out = "".join(capfd.readouterr().err.split())
    # Only the user-visible substring and the absence of a traceback matter.
    assert "TCPport" in out or f"TCPport{taken}" in out
    assert "alreadyinuse" in out
    assert "Traceback" not in out
