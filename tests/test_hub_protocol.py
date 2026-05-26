"""Tests for ``rtl_buddy.hub.protocol`` — envelope codec + schema."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from rtl_buddy.hub import protocol
from rtl_buddy.hub.protocol import (
    Diagnostic,
    Envelope,
    HubProtocolError,
    Kind,
    Origin,
    decode,
    encode,
    make_diagnostics_set,
    make_error,
    make_hello,
    make_welcome,
    new_id,
)


# ---------------------------------------------------------------------------
# round trips
# ---------------------------------------------------------------------------


def test_decode_encode_round_trip_event():
    payload = {
        "v": 1,
        "id": str(uuid.uuid4()),
        "origin": "view",
        "kind": "event",
        "type": "selection_changed",
        "payload": {"instance_path": "top.u_fifo.u_wr_ptr"},
    }
    env = decode(json.dumps(payload))
    assert env.origin is Origin.VIEW
    assert env.kind is Kind.EVENT
    assert env.type == "selection_changed"
    assert env.payload == payload["payload"]
    assert json.loads(encode(env)) == payload


def test_decode_accepts_dict_directly():
    payload = {
        "v": 1,
        "id": str(uuid.uuid4()),
        "origin": "wave",
        "kind": "event",
        "type": "cursor_time_changed",
        "payload": {"t_fs": "12500000"},
    }
    env = decode(payload)
    assert env.origin is Origin.WAVE
    assert env.payload == {"t_fs": "12500000"}


def test_encode_drops_payload_when_none():
    env = Envelope(
        origin=Origin.CLI, kind=Kind.EVENT, type="bye", id=new_id(), payload=None
    )
    obj = json.loads(encode(env))
    assert "payload" not in obj


# ---------------------------------------------------------------------------
# schema enforcement
# ---------------------------------------------------------------------------


def test_decode_rejects_unknown_origin():
    raw = json.dumps(
        {
            "v": 1,
            "id": str(uuid.uuid4()),
            "origin": "nope",
            "kind": "event",
            "type": "selection_changed",
            "payload": {"instance_path": "top"},
        }
    )
    with pytest.raises(HubProtocolError):
        decode(raw)


def test_decode_rejects_protocol_version_mismatch():
    raw = json.dumps(
        {
            "v": 2,
            "id": str(uuid.uuid4()),
            "origin": "view",
            "kind": "event",
            "type": "selection_changed",
            "payload": {"instance_path": "top"},
        }
    )
    with pytest.raises(HubProtocolError):
        decode(raw)


def test_decode_rejects_bad_uuid():
    raw = json.dumps(
        {
            "v": 1,
            "id": "not-a-uuid",
            "origin": "view",
            "kind": "event",
            "type": "selection_changed",
            "payload": {"instance_path": "top"},
        }
    )
    with pytest.raises(HubProtocolError) as ei:
        decode(raw)
    assert "/id" in ei.value.json_pointer


def test_decode_rejects_missing_payload_field():
    """selection_changed requires payload.instance_path."""

    raw = json.dumps(
        {
            "v": 1,
            "id": str(uuid.uuid4()),
            "origin": "view",
            "kind": "event",
            "type": "selection_changed",
            "payload": {},
        }
    )
    with pytest.raises(HubProtocolError):
        decode(raw)


def test_decode_rejects_additional_payload_properties():
    """additionalProperties: false on every payload subschema."""

    raw = json.dumps(
        {
            "v": 1,
            "id": str(uuid.uuid4()),
            "origin": "view",
            "kind": "event",
            "type": "selection_changed",
            "payload": {"instance_path": "top", "extra": True},
        }
    )
    with pytest.raises(HubProtocolError):
        decode(raw)


def test_decode_rejects_wrong_kind_for_event_type():
    raw = json.dumps(
        {
            "v": 1,
            "id": str(uuid.uuid4()),
            "origin": "view",
            "kind": "request",  # selection_changed is event-only
            "type": "selection_changed",
            "payload": {"instance_path": "top"},
        }
    )
    with pytest.raises(HubProtocolError):
        decode(raw)


def test_decode_silently_accepts_unknown_type():
    """§11: unknown types MUST be silently dropped at DEBUG.

    The codec accepts them; downstream routing is what discards them.
    """

    raw = json.dumps(
        {
            "v": 1,
            "id": str(uuid.uuid4()),
            "origin": "view",
            "kind": "event",
            "type": "future_v2_event",
            "payload": {"anything": "goes"},
        }
    )
    env = decode(raw)
    assert env.type == "future_v2_event"


def test_decode_rejects_malformed_json():
    with pytest.raises(HubProtocolError):
        decode("{not-json")


# ---------------------------------------------------------------------------
# encode-side validation
# ---------------------------------------------------------------------------


def test_encode_rejects_invalid_envelope():
    """A bug in a caller surfaces here, not on the remote peer."""

    env = Envelope(
        origin=Origin.VIEW,
        kind=Kind.EVENT,
        type="selection_changed",
        id="not-a-uuid",
        payload={"instance_path": "top"},
    )
    with pytest.raises(HubProtocolError):
        encode(env)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_make_hello_produces_valid_envelope():
    env = make_hello(client=Origin.VIEW, version="0.1.0", capabilities=["x", "y"])
    raw = encode(env)
    assert decode(raw) == env


def test_make_welcome_carries_request_id():
    hello = make_hello(client=Origin.VIEW, version="0.1.0", capabilities=[])
    welcome = make_welcome(
        in_reply_to=hello.id,
        server_version="0.1.0",
        registered_clients=[Origin.WAVE],
    )
    assert welcome.id == hello.id
    assert welcome.payload == {
        "server_version": "0.1.0",
        "registered_clients": ["wave"],
    }


def test_make_error_carries_request_id_when_in_reply_to():
    err = make_error(
        origin=Origin.CLI,
        code="unresolvable",
        message="no scope",
        context={"instance_path": "top.x"},
        in_reply_to="11111111-1111-1111-1111-111111111111",
    )
    assert err.id == "11111111-1111-1111-1111-111111111111"
    assert err.payload["code"] == "unresolvable"
    encode(err)  # must pass schema


def test_make_error_generates_id_when_spontaneous():
    err = make_error(origin=Origin.CLI, code="bad_request", message="oops")
    uuid.UUID(err.id)  # must parse


def test_new_id_is_uuid4():
    val = new_id()
    parsed = uuid.UUID(val)
    assert parsed.version == 4


# ---------------------------------------------------------------------------
# vendored schema drift
# ---------------------------------------------------------------------------


def test_vendored_schema_has_expected_types():
    """Catch accidental schema drift: every spec ``type`` is present."""

    schema = protocol.schema()
    types_seen: set[str] = set()
    for branch in schema.get("allOf", []):
        const = branch.get("if", {}).get("properties", {}).get("type", {}).get("const")
        if const is not None:
            types_seen.add(const)
    expected = {
        "selection_changed",
        "signal_selected",
        "cursor_time_changed",
        "scope_changed",
        "source_focused",
        "open_source",
        "wave_add_variables",
        "wave_set_scope",
        "wave_set_cursor",
        "wave_set_viewport",
        "wave_zoom_to_range",
        "wave_zoom_to_fit",
        "wave_values_changed",
        "view_pan_to",
        "view_capture",
        "view_overlay_set",
        "wave_set_viewport",
        "wave_zoom_to_range",
        "wave_zoom_to_fit",
        "resolve_view_to_wave",
        "resolve_wave_to_view",
        "resolve_signal_to_view",
        "state_snapshot",
        "hello",
        "welcome",
        "bye",
        "peer_joined",
        "view_changed",
        "error",
        "diagnostics_set",
    }
    assert types_seen == expected


# ---------------------------------------------------------------------------
# diagnostics_set
# ---------------------------------------------------------------------------


def test_make_diagnostics_set_round_trips_minimal_and_full_items():
    env = make_diagnostics_set(
        origin=Origin.CLI,
        source="rtl-buddy-cdc",
        items=[
            Diagnostic(file="/abs/a.sv", line=1, severity="error", message="m"),
            Diagnostic(
                file="/abs/b.sv",
                line=10,
                col=4,
                end_line=10,
                end_col=18,
                severity="warning",
                code="CDC-002",
                message="depth too shallow",
            ),
        ],
    )
    line = encode(env)
    back = decode(line)
    assert back.type == "diagnostics_set"
    assert back.kind is Kind.EVENT
    assert back.payload["source"] == "rtl-buddy-cdc"
    assert len(back.payload["items"]) == 2
    minimal, full = back.payload["items"]
    assert minimal == {
        "file": "/abs/a.sv",
        "line": 1,
        "severity": "error",
        "message": "m",
    }
    assert full["code"] == "CDC-002"
    assert full["end_col"] == 18


def test_make_diagnostics_set_accepts_dict_items():
    env = make_diagnostics_set(
        origin=Origin.CLI,
        source="manual",
        items=[{"file": "/x.sv", "line": 5, "severity": "info", "message": "ok"}],
    )
    assert encode(env)  # validates via schema


def test_diagnostics_set_empty_items_is_legal_clear():
    env = make_diagnostics_set(origin=Origin.CLI, source="rtl-buddy-cdc", items=[])
    back = decode(encode(env))
    assert back.payload == {"source": "rtl-buddy-cdc", "items": []}


def test_diagnostics_set_carries_optional_instance_path():
    """The view-side resolver (rtl-buddy-view#82) prefers
    ``item.instance_path`` over file+line range matching. Make sure
    the dataclass + ``make_diagnostics_set`` + schema round-trip
    preserve the hint when set, and omit it cleanly when None."""

    env = make_diagnostics_set(
        origin=Origin.CLI,
        source="claude-analysis",
        items=[
            Diagnostic(
                file="/abs/a.sv",
                line=42,
                severity="warning",
                code="WAVE-1",
                message="wr_ptr_q sampled while ce==0",
                instance_path="top.u_dma",
            ),
            Diagnostic(file="/abs/b.sv", line=1, severity="info", message="ok"),
        ],
    )
    back = decode(encode(env))
    pinned, unpinned = back.payload["items"]
    assert pinned["instance_path"] == "top.u_dma"
    assert "instance_path" not in unpinned


def test_diagnostics_set_rejects_empty_instance_path():
    """Schema clamps to minLength: 1 so a producer can't send an
    empty string and trip the consumer's fast path on garbage."""

    with pytest.raises(HubProtocolError):
        encode(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="diagnostics_set",
                id=new_id(),
                payload={
                    "source": "x",
                    "items": [
                        {
                            "file": "/a.sv",
                            "line": 1,
                            "severity": "info",
                            "message": "m",
                            "instance_path": "",
                        }
                    ],
                },
            )
        )


def test_diagnostics_set_rejects_bad_severity():
    with pytest.raises(HubProtocolError):
        encode(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="diagnostics_set",
                id=new_id(),
                payload={
                    "source": "x",
                    "items": [
                        {
                            "file": "/x.sv",
                            "line": 1,
                            "severity": "fatal",
                            "message": "m",
                        }
                    ],
                },
            )
        )


def test_diagnostics_set_rejects_missing_required_item_field():
    with pytest.raises(HubProtocolError):
        encode(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="diagnostics_set",
                id=new_id(),
                payload={
                    "source": "x",
                    "items": [{"file": "/x.sv", "severity": "error", "message": "m"}],
                },
            )
        )


def test_diagnostics_set_rejects_line_zero():
    with pytest.raises(HubProtocolError):
        encode(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="diagnostics_set",
                id=new_id(),
                payload={
                    "source": "x",
                    "items": [
                        {
                            "file": "/x.sv",
                            "line": 0,
                            "severity": "error",
                            "message": "m",
                        }
                    ],
                },
            )
        )


def test_vendored_schema_matches_source_when_view_repo_present():
    """Schema is vendored — drift detection when the view repo is a sibling.

    The source of truth lives in ``rtl-buddy/rtl-buddy-view``. CI for
    rtl_buddy does not clone that repo, so this test no-ops there; it
    fires locally when both checkouts are side-by-side.
    """

    sibling = Path(__file__).resolve().parents[2] / "rtl-buddy-view"
    src_schema = sibling / "schemas" / "hub-protocol-v1.json"
    if not src_schema.is_file():
        pytest.skip("rtl-buddy-view sibling checkout not present")

    vendored = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "rtl_buddy"
        / "hub"
        / "schema"
        / "hub-protocol-v1.json"
    )
    assert vendored.read_bytes() == src_schema.read_bytes(), (
        "vendored schema has drifted from the rtl-buddy-view source. "
        "Re-copy and commit; the wire contract is owned by Phase 10a."
    )
