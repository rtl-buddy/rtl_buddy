"""Wire envelope codec for the rtl-buddy-hub protocol v1.

The spec lives in ``docs/hub-protocol.md`` of the
``rtl-buddy/rtl-buddy-view`` repo and is enforced by the JSON Schema at
``schemas/hub-protocol-v1.json`` (vendored alongside this module as
:mod:`rtl_buddy.hub.schema`).

This module exposes:

* :data:`PROTOCOL_VERSION` — the integer ``v`` field shared by every
  envelope; mismatch is a fatal protocol error.
* :class:`Origin` / :class:`Kind` — the closed enums from the spec.
* :class:`Envelope` — frozen dataclass mirroring §2 of the spec.
* :func:`encode` / :func:`decode` — round-trip ``Envelope`` ↔ JSON
  with schema validation on both sides.
* :func:`new_id` — UUID4 generator used by message creators.

The schema is loaded once at import time; both encode and decode call
into the same validator so a malformed payload fails fast with a
:class:`HubProtocolError`. The validator is intentionally strict
(``additionalProperties: false`` on every payload object); unknown
fields are caller bugs, not forward-compatibility points.

Unknown ``type`` strings are NOT a schema error — §11 of the spec
requires clients to silently drop unknown types so a v1 client and a
future v1.1 hub can co-exist. The validator only enforces the envelope
shape (``v``, ``id``, ``origin``, ``kind``, ``type``); ``type``-specific
payload subschemas are matched conditionally via ``if/then`` and skip
unknown types.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator


PROTOCOL_VERSION: int = 1


class Origin(str, Enum):
    """``origin`` field — the conceptual originator of a message."""

    VIEW = "view"
    WAVE = "wave"
    SRC = "src"
    CLI = "cli"


class Kind(str, Enum):
    """``kind`` field — envelope category."""

    EVENT = "event"
    REQUEST = "request"
    RESPONSE = "response"
    ERROR = "error"


class HubProtocolError(Exception):
    """Raised when a wire payload violates the v1 envelope or schema.

    Carries the offending JSON pointer in :attr:`json_pointer` when the
    failure was reported by the JSON Schema validator (else ``""``).
    Mirrors the ``bad_request`` error code from the protocol's error
    catalog.
    """

    def __init__(self, message: str, *, json_pointer: str = "") -> None:
        super().__init__(message)
        self.json_pointer = json_pointer


@dataclass(frozen=True, slots=True)
class Envelope:
    """A protocol message after parsing — mirrors §2 of the spec.

    ``id`` is the request/response correlation key and the dedupe key
    for the loop-prevention LRU (§6). Callers SHOULD generate new IDs
    with :func:`new_id`; the codec validates the canonical UUID shape
    but does not generate IDs on its own.
    """

    origin: Origin
    kind: Kind
    type: str
    id: str
    payload: Any = None
    v: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible dict shape used by :func:`encode`."""

        out: dict[str, Any] = {
            "v": self.v,
            "id": self.id,
            "origin": self.origin.value,
            "kind": self.kind.value,
            "type": self.type,
        }
        if self.payload is not None:
            out["payload"] = self.payload
        return out


def new_id() -> str:
    """Return a fresh canonical UUID4 string for use as ``id``."""

    return str(uuid.uuid4())


_SCHEMA_RESOURCE = "hub-protocol-v1.json"


def _load_schema() -> dict[str, Any]:
    text = (
        resources.files("rtl_buddy.hub.schema")
        .joinpath(_SCHEMA_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


_SCHEMA: dict[str, Any] = _load_schema()
_VALIDATOR: Draft202012Validator = Draft202012Validator(_SCHEMA)


def schema() -> dict[str, Any]:
    """Return a deep copy of the vendored JSON Schema.

    Useful for tests that want to drift-check against the source-of-
    truth copy in ``rtl-buddy-view/schemas/hub-protocol-v1.json``.
    """

    return json.loads(json.dumps(_SCHEMA))


def _validate(obj: dict[str, Any]) -> None:
    errors = sorted(_VALIDATOR.iter_errors(obj), key=lambda e: e.path)
    if not errors:
        return
    first = errors[0]
    pointer = "/" + "/".join(str(p) for p in first.absolute_path)
    raise HubProtocolError(
        f"envelope failed schema validation at {pointer}: {first.message}",
        json_pointer=pointer,
    )


def decode(raw: str | bytes | dict[str, Any]) -> Envelope:
    """Parse a wire payload into an :class:`Envelope`.

    Accepts either a JSON string/bytes (the line-delimited TCP
    transport) or a pre-parsed dict (the WebSocket layer, which has
    already framed). Raises :class:`HubProtocolError` if the payload is
    not valid JSON, fails the schema, or carries the wrong protocol
    version.
    """

    if isinstance(raw, (str, bytes)):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HubProtocolError(f"not valid JSON: {exc.msg}") from exc
    else:
        obj = raw

    if not isinstance(obj, dict):
        raise HubProtocolError("envelope must be a JSON object")

    _validate(obj)

    v = obj["v"]
    if v != PROTOCOL_VERSION:
        raise HubProtocolError(
            f"protocol mismatch: expected v={PROTOCOL_VERSION}, got v={v}"
        )

    return Envelope(
        origin=Origin(obj["origin"]),
        kind=Kind(obj["kind"]),
        type=obj["type"],
        id=obj["id"],
        payload=obj.get("payload"),
        v=v,
    )


def encode(envelope: Envelope) -> str:
    """Serialize an :class:`Envelope` to a wire-ready JSON string.

    The line-delimited TCP transport expects exactly one envelope per
    line; this function returns the JSON without a trailing newline so
    the transport layer can frame consistently across NDJSON and
    WebSocket. Validates against the schema before returning so a
    bug in caller code surfaces here, not at the remote peer.
    """

    obj = envelope.to_dict()
    _validate(obj)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def make_error(
    *,
    origin: Origin,
    code: str,
    message: str,
    context: dict[str, Any] | None = None,
    in_reply_to: str | None = None,
) -> Envelope:
    """Construct an ``error`` envelope per §3 of the spec.

    ``in_reply_to`` is the request ``id`` to echo back; when ``None``
    (e.g. a spontaneous error not tied to a request) a fresh UUID is
    generated. ``code`` MUST be one of the strings enumerated in the
    spec's error table; otherwise the schema validator rejects it.
    """

    payload: dict[str, Any] = {"code": code, "message": message}
    if context is not None:
        payload["context"] = context
    return Envelope(
        origin=origin,
        kind=Kind.ERROR,
        type="error",
        id=in_reply_to or new_id(),
        payload=payload,
    )


def make_hello(
    *,
    client: Origin,
    version: str,
    capabilities: list[str],
) -> Envelope:
    """Construct a ``hello`` request envelope (§4.3, §11)."""

    return Envelope(
        origin=client,
        kind=Kind.REQUEST,
        type="hello",
        id=new_id(),
        payload={
            "client": client.value,
            "version": version,
            "capabilities": capabilities,
        },
    )


def make_welcome(
    *,
    in_reply_to: str,
    server_version: str,
    registered_clients: list[Origin],
) -> Envelope:
    """Construct the hub's ``welcome`` response envelope (§4.3)."""

    return Envelope(
        origin=Origin.CLI,
        kind=Kind.RESPONSE,
        type="welcome",
        id=in_reply_to,
        payload={
            "server_version": server_version,
            "registered_clients": [c.value for c in registered_clients],
        },
    )


__all__ = [
    "PROTOCOL_VERSION",
    "Origin",
    "Kind",
    "Envelope",
    "HubProtocolError",
    "decode",
    "encode",
    "new_id",
    "schema",
    "make_error",
    "make_hello",
    "make_welcome",
]
