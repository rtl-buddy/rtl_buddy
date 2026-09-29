"""Wire envelope codec for hub protocol v1.

The spec is ``docs/hub-protocol.md`` in ``rtl-buddy/rtl-buddy-sch``, enforced by the JSON Schema vendored as :mod:`rtl_buddy.hub.schema`. The schema is strict (``additionalProperties: false`` on payloads) and is applied on both :func:`encode` and :func:`decode`. Unknown ``type`` strings are valid; only the envelope shape is checked for them, and clients drop them (§11).
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
    """The ``origin`` field: which client sent a message. The hub allows one client per origin."""

    VIEW = "view"
    WAVE = "wave"
    SRC = "src"
    CLI = "cli"
    NOTEBOOK = "notebook"
    GRAPH = "graph"
    """The design-knowledge-graph pane (``GET /gph``). Separate from ``view`` so both can be open at once."""

    COV = "cov"
    """The coverage pane (``GET /cov``). Separate origin so it can be open alongside the other panes."""

    PHYS = "phys"
    """The synth and power pane (``GET /phy``). The origin is ``phys`` although the route and label are ``phy``."""


class Kind(str, Enum):
    """The ``kind`` field: envelope category."""

    EVENT = "event"
    REQUEST = "request"
    RESPONSE = "response"
    ERROR = "error"


class HubProtocolError(Exception):
    """Raised when a wire payload violates the v1 envelope or schema.

    :attr:`json_pointer` is the failing location when the schema validator reported it, else ``""``.
    """

    def __init__(self, message: str, *, json_pointer: str = "") -> None:
        super().__init__(message)
        self.json_pointer = json_pointer


@dataclass(frozen=True, slots=True)
class Envelope:
    """A parsed protocol message (§2 of the spec).

    ``id`` correlates requests and responses and is the loop-prevention dedupe key (§6). Generate it with :func:`new_id`; the codec validates but never generates it.
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
    """Return a deep copy of the vendored JSON Schema."""

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
    """Parse a JSON string, bytes or pre-parsed dict into an :class:`Envelope`.

    Raises :class:`HubProtocolError` if the payload is not valid JSON, fails the schema, or has the wrong protocol version.
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
    """Serialize an :class:`Envelope` to compact JSON without a trailing newline.

    Raises :class:`HubProtocolError` if the envelope fails the schema.
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
    """Build an ``error`` envelope.

    ``in_reply_to`` is the request ``id`` to echo; with ``None`` a fresh id is generated. ``code`` must be in the spec's error table or encoding fails.
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
    """Build a ``hello`` request envelope."""

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
    """Build the hub's ``welcome`` response envelope."""

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


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One finding in a ``diagnostics_set`` payload; ``None`` fields are omitted on the wire."""

    file: str
    line: int
    severity: str  # "error" | "warning" | "info" | "hint"
    message: str
    col: int | None = None
    end_line: int | None = None
    end_col: int | None = None
    code: str | None = None
    instance_path: str | None = None
    """The ``view.json`` instance path this finding belongs to. Lets consumers skip file-and-line resolution, which cannot locate findings in an instantiated module's own body."""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "message": self.message,
        }
        if self.col is not None:
            out["col"] = self.col
        if self.end_line is not None:
            out["end_line"] = self.end_line
        if self.end_col is not None:
            out["end_col"] = self.end_col
        if self.code is not None:
            out["code"] = self.code
        if self.instance_path is not None:
            out["instance_path"] = self.instance_path
        return out


def make_diagnostics_set(
    *,
    origin: Origin,
    source: str,
    items: list[Diagnostic] | list[dict[str, Any]],
) -> Envelope:
    """Build a ``diagnostics_set`` event envelope.

    ``items`` holds :class:`Diagnostic` objects or ready-made dicts. An empty list clears the source's diagnostics.
    """

    payload_items: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Diagnostic):
            payload_items.append(item.to_dict())
        else:
            payload_items.append(dict(item))
    return Envelope(
        origin=origin,
        kind=Kind.EVENT,
        type="diagnostics_set",
        id=new_id(),
        payload={"source": source, "items": payload_items},
    )


__all__ = [
    "PROTOCOL_VERSION",
    "Origin",
    "Kind",
    "Envelope",
    "Diagnostic",
    "HubProtocolError",
    "decode",
    "encode",
    "new_id",
    "schema",
    "make_error",
    "make_hello",
    "make_welcome",
    "make_diagnostics_set",
]
