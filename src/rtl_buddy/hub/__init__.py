"""Hub daemon package: mediates selection and cursor messages between the schematic, waveform and source views.

Submodules: ``protocol`` (wire envelope codec), ``config`` (``.rtl-buddy/hub.toml``), ``discovery`` (``.rtl-buddy/hub.json``), ``state`` (selection and cursor cache), ``cli`` (``rb hub``).
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
