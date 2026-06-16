"""rtl-buddy-hub daemon package.

Phase 10b implementation (`rtl-buddy/rtl_buddy#115`) of the wire
contract frozen by `rtl-buddy/rtl-buddy-view#19` —
``docs/hub-protocol.md`` v1 in the rtl-buddy-view repo.

The daemon mediates messages between three views of a SystemVerilog
design (schematic / waveform / source) so a click in one is reflected
in the others. See the spec for the full picture; this package contains
the Python implementation:

* :mod:`rtl_buddy.hub.protocol` — wire envelope codec.
* :mod:`rtl_buddy.hub.config`   — ``.rtl-buddy/hub.toml`` loader.
* :mod:`rtl_buddy.hub.discovery`— ``.rtl-buddy/hub.json`` lifecycle.
* :mod:`rtl_buddy.hub.state`    — in-memory selection / cursor cache.
* :mod:`rtl_buddy.hub.cli`      — ``rb hub`` Typer subcommand surface.
"""

from .protocol import (
    HubProtocolError,
    Origin,
    Kind,
    Envelope,
    decode,
    encode,
    new_id,
    PROTOCOL_VERSION,
)

__all__ = [
    "HubProtocolError",
    "Origin",
    "Kind",
    "Envelope",
    "decode",
    "encode",
    "new_id",
    "PROTOCOL_VERSION",
]
