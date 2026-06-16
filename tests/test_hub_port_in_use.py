"""Tests for the EADDRINUSE clean-error path in ``rb hub start``.

Pinning a port (via CLI flag or hub.toml) is common; landing on a port
that's already held by another process should produce a one-line
"port X already in use" message, not a websockets/asyncio traceback.
"""

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
    """Bind a TCP listener on (127.0.0.1, port) so any other bind on
    the same port hits EADDRINUSE. Yields the port back."""
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
    """Direct exercise of the helper that wraps server/viewer .start()."""

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
    """Non-EADDRINUSE OSErrors should propagate unchanged; we only
    translate the specific bind-conflict case."""

    async def boom():
        raise OSError(13, "permission denied")

    with pytest.raises(OSError) as info:
        await _start_listener(boom(), role="TCP", port=42)
    assert info.value.errno == 13


def test_serve_returns_1_and_emits_clean_message_on_eaddrinuse(tmp_path: Path, capfd):
    """End-to-end: ``serve()`` with a pinned listen_port that's already
    held returns exit code 1 and prints a one-line message — no
    traceback. Mirrors what ``rb hub start --listen-port N`` does at
    the CLI surface."""

    with _hold_port(0) as taken:
        cfg = HubConfig(
            hub=HubServerConfig(listen_port=taken, http_port=0),
            mapping=HubMappingConfig(),
        )
        rc = serve(tmp_path, cfg, serve_viewer=False)

    assert rc == 1
    out = "".join(capfd.readouterr().err.split())
    # Either ordering of "TCP port" + the port number is fine; we just
    # need the user-visible substring and no Traceback.
    assert "TCPport" in out or f"TCPport{taken}" in out
    assert "alreadyinuse" in out
    assert "Traceback" not in out
