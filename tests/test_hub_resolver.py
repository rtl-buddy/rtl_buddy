"""Tests for ``rtl_buddy.hub.resolver``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_buddy.hub.config import HubMappingConfig, SignalAlias
from rtl_buddy.hub.resolver import (
    ResolverError,
    SignalDriver,
    SourceAnchor,
    ViewModel,
    default_view_json_path,
    resolver_from_paths,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sample_view_json(top: str = "counter") -> dict:
    """Mirrors the live rtl-buddy-view JSON output shape (counter fixture)."""

    return {
        "schema_version": "1.0",
        "tool": {"name": "rtl-buddy-view", "version": "0.1.0"},
        "design": {"top": top},
        "nodes": [
            {
                "instance_path": top,
                "module_name": top,
                "instance_name": None,
                "is_blackbox": False,
                "param_overrides": [],
                "port_connections": [],
                "location": {
                    "file": "/abs/rtl/counter.sv",
                    "start_line": 5,
                    "start_column": 1,
                    "end_line": 12,
                    "end_column": 10,
                },
                "clock": None,
                "crossings_in": [],
            },
            {
                "instance_path": f"{top}.u_ff",
                "module_name": "counter_ff",
                "instance_name": "u_ff",
                "is_blackbox": False,
                "param_overrides": [],
                "port_connections": [
                    {"port_name": "clk", "net_expr_text": "clk"},
                    {"port_name": "q", "net_expr_text": "q"},
                ],
                "location": {
                    "file": "/abs/rtl/counter.sv",
                    "start_line": 10,
                    "start_column": 16,
                    "end_line": 10,
                    "end_column": 39,
                },
                "clock": None,
                "crossings_in": [],
            },
            {
                "instance_path": f"{top}.u_x",
                "module_name": "sub_x",
                "instance_name": "u_x",
                "is_blackbox": True,
                "param_overrides": [],
                "port_connections": [
                    {"port_name": "clk", "net_expr_text": "clk"},
                ],
                "location": {
                    "file": "/abs/rtl/counter.sv",
                    "start_line": 11,
                    "start_column": 16,
                    "end_line": 11,
                    "end_column": 32,
                },
                "clock": None,
                "crossings_in": [],
            },
        ],
        "edges": [
            {"parent": top, "child": f"{top}.u_ff"},
            {"parent": top, "child": f"{top}.u_x"},
        ],
    }


def _write_view_json(tmp_path: Path, payload: dict | None = None) -> Path:
    if payload is None:
        payload = _sample_view_json()
    p = tmp_path / "view.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# ViewModel.from_dict
# ---------------------------------------------------------------------------


def test_from_dict_parses_minimal_payload(tmp_path: Path):
    model = ViewModel.from_dict(_sample_view_json())
    assert model.top == "counter"
    assert set(model.nodes_by_path) == {"counter", "counter.u_ff", "counter.u_x"}
    assert model.edges_parent_to_children["counter"] == (
        "counter.u_ff",
        "counter.u_x",
    )

    u_ff = model.nodes_by_path["counter.u_ff"]
    assert u_ff.port_connections == (("clk", "clk"), ("q", "q"))
    assert u_ff.location == SourceAnchor(
        file="/abs/rtl/counter.sv", line=10, col=16, end_line=10, end_col=39
    )


def test_from_dict_rejects_unsupported_schema_major():
    payload = _sample_view_json()
    payload["schema_version"] = "2.0"
    with pytest.raises(ResolverError, match="schema major 2"):
        ViewModel.from_dict(payload)


def test_from_dict_rejects_unparseable_schema_version():
    payload = _sample_view_json()
    payload["schema_version"] = "abc"
    with pytest.raises(ResolverError):
        ViewModel.from_dict(payload)


def test_from_dict_rejects_missing_top():
    payload = _sample_view_json()
    del payload["design"]
    with pytest.raises(ResolverError, match="design.top"):
        ViewModel.from_dict(payload)


def test_from_dict_skips_malformed_node_entries():
    payload = _sample_view_json()
    payload["nodes"].append({"no_instance_path": True})
    payload["nodes"].append(
        {
            "instance_path": "counter.u_y",
            "port_connections": [
                {"port_name": "clk"},  # missing net_expr_text
                {"port_name": "rst", "net_expr_text": 42},  # wrong type
                {"port_name": "go", "net_expr_text": "go"},  # ok
            ],
            "location": "not a dict",
        }
    )
    model = ViewModel.from_dict(payload)
    assert "counter.u_y" in model.nodes_by_path
    u_y = model.nodes_by_path["counter.u_y"]
    assert u_y.port_connections == (("go", "go"),)
    assert u_y.location is None


# ---------------------------------------------------------------------------
# view ↔ wave transforms
# ---------------------------------------------------------------------------


def test_view_to_wave_strips_top_and_prepends_prefix(tmp_path: Path):
    resolver = resolver_from_paths(
        view_json_path=_write_view_json(tmp_path), tb_prefix="tb.dut."
    )
    assert resolver.view_to_wave("counter.u_ff") == "tb.dut.u_ff"
    assert resolver.view_to_wave("counter") == "tb.dut."


def test_view_to_wave_returns_none_for_unknown_path(tmp_path: Path):
    resolver = resolver_from_paths(
        view_json_path=_write_view_json(tmp_path), tb_prefix="tb.dut."
    )
    assert resolver.view_to_wave("counter.u_dbg") is None


def test_view_to_wave_handles_empty_prefix(tmp_path: Path):
    resolver = resolver_from_paths(
        view_json_path=_write_view_json(tmp_path), tb_prefix=""
    )
    assert resolver.view_to_wave("counter.u_ff") == "u_ff"


def test_view_to_wave_uses_aliases_before_prefix(tmp_path: Path):
    resolver = resolver_from_paths(
        view_json_path=_write_view_json(tmp_path),
        tb_prefix="tb.dut.",
        signal_aliases=[
            SignalAlias(wave="tb.dut.legacy_ff", view="counter.u_ff"),
        ],
    )
    assert resolver.view_to_wave("counter.u_ff") == "tb.dut.legacy_ff"


def test_wave_to_view_strips_prefix_and_prepends_top(tmp_path: Path):
    resolver = resolver_from_paths(
        view_json_path=_write_view_json(tmp_path), tb_prefix="tb.dut."
    )
    assert resolver.wave_to_view("tb.dut.u_ff") == "counter.u_ff"


def test_wave_to_view_uses_aliases(tmp_path: Path):
    resolver = resolver_from_paths(
        view_json_path=_write_view_json(tmp_path),
        tb_prefix="tb.dut.",
        signal_aliases=[
            SignalAlias(wave="tb.dut.legacy_ff", view="counter.u_ff"),
        ],
    )
    assert resolver.wave_to_view("tb.dut.legacy_ff") == "counter.u_ff"


def test_wave_to_view_returns_none_when_prefix_mismatch(tmp_path: Path):
    resolver = resolver_from_paths(
        view_json_path=_write_view_json(tmp_path), tb_prefix="tb.dut."
    )
    assert resolver.wave_to_view("foo.bar") is None


def test_wave_to_view_returns_none_when_path_unknown(tmp_path: Path):
    resolver = resolver_from_paths(
        view_json_path=_write_view_json(tmp_path), tb_prefix="tb.dut."
    )
    assert resolver.wave_to_view("tb.dut.u_dbg") is None


# ---------------------------------------------------------------------------
# view → src
# ---------------------------------------------------------------------------


def test_view_to_src_returns_anchor(tmp_path: Path):
    resolver = resolver_from_paths(
        view_json_path=_write_view_json(tmp_path), tb_prefix="tb.dut."
    )
    anchor = resolver.view_to_src("counter.u_ff")
    assert anchor == SourceAnchor(
        file="/abs/rtl/counter.sv", line=10, col=16, end_line=10, end_col=39
    )


def test_view_to_src_missing_path_returns_none(tmp_path: Path):
    resolver = resolver_from_paths(
        view_json_path=_write_view_json(tmp_path), tb_prefix="tb.dut."
    )
    assert resolver.view_to_src("counter.nope") is None


def test_view_to_src_returns_none_without_view_json(tmp_path: Path):
    resolver = resolver_from_paths(view_json_path=None)
    assert resolver.view_to_src("counter.u_ff") is None


# ---------------------------------------------------------------------------
# canonical view-json-v1 schema (top + id + source) parsing
# ---------------------------------------------------------------------------


def _canonical_view_json() -> dict:
    """Matches rtl-buddy-view's documented schema (view-json-v1.md):
    flat ``top``, per-node ``id`` + ``source`` with start+end positions,
    ``ports`` (not legacy ``port_connections``), ``{from, to}`` edges.
    """
    return {
        "schema_version": "1.0",
        "tool": {"name": "rtl-buddy-view", "version": "0.1.0"},
        "top": "counter",
        "nodes": [
            {
                "id": "counter",
                "module": "counter",
                "source": {
                    "file": "/abs/rtl/counter.sv",
                    "start_line": 5,
                    "start_column": 1,
                    "end_line": 50,
                    "end_column": 10,
                },
                "ports": [],
            },
            {
                "id": "counter.u_ff",
                "module": "counter_ff",
                "source": {
                    "file": "/abs/rtl/counter.sv",
                    "start_line": 20,
                    "start_column": 16,
                    "end_line": 25,
                    "end_column": 4,
                },
                "ports": [
                    {"name": "clk", "expr": "clk"},
                    {"name": "q", "expr": "q"},
                ],
            },
        ],
        "edges": [{"from": "counter", "to": "counter.u_ff"}],
    }


def test_from_dict_parses_canonical_schema():
    """The schema rtl-buddy-view actually emits — `top`, `id`, `source`,
    `ports`, `{from, to}` edges. The legacy fixture covered the other
    branch."""

    model = ViewModel.from_dict(_canonical_view_json())
    assert model.top == "counter"
    assert set(model.nodes_by_path) == {"counter", "counter.u_ff"}
    u_ff = model.nodes_by_path["counter.u_ff"]
    assert u_ff.location == SourceAnchor(
        file="/abs/rtl/counter.sv", line=20, col=16, end_line=25, end_col=4
    )
    assert u_ff.port_connections == (("clk", "clk"), ("q", "q"))
    assert model.edges_parent_to_children["counter"] == ("counter.u_ff",)


# ---------------------------------------------------------------------------
# src → view  (cursor file/line/col → instance path)
# ---------------------------------------------------------------------------


def _write_canonical(tmp_path: Path) -> Path:
    out = tmp_path / "view.json"
    out.write_text(json.dumps(_canonical_view_json()))
    return out


def test_src_to_view_returns_containing_instance(tmp_path: Path):
    resolver = resolver_from_paths(view_json_path=_write_canonical(tmp_path))
    # Line 22 falls inside u_ff's [20, 25] range AND counter's [5, 50]
    # range. Both qualify; the smaller (u_ff) wins on the size tiebreak.
    matches = resolver.src_to_view(file="/abs/rtl/counter.sv", line=22)
    assert matches == ("counter.u_ff", "counter")


def test_src_to_view_returns_outer_instance_when_only_outer_matches(
    tmp_path: Path,
):
    resolver = resolver_from_paths(view_json_path=_write_canonical(tmp_path))
    # Line 7 is inside counter's [5, 50] but outside u_ff's [20, 25].
    matches = resolver.src_to_view(file="/abs/rtl/counter.sv", line=7)
    assert matches == ("counter",)


def test_src_to_view_empty_when_line_outside_all_ranges(tmp_path: Path):
    resolver = resolver_from_paths(view_json_path=_write_canonical(tmp_path))
    # Line 999 is past every node's end_line.
    assert resolver.src_to_view(file="/abs/rtl/counter.sv", line=999) == ()


def test_src_to_view_empty_when_file_does_not_match(tmp_path: Path):
    resolver = resolver_from_paths(view_json_path=_write_canonical(tmp_path))
    assert resolver.src_to_view(file="/abs/rtl/other.sv", line=22) == ()


def test_src_to_view_normalises_path(tmp_path: Path):
    # The cursor's file is an absolute path; view.json's `source.file`
    # was emitted as a relative path. Resolver normalises both via
    # Path.resolve() so they compare equal.
    rel = tmp_path / "rtl"
    rel.mkdir()
    (rel / "counter.sv").write_text("// content")
    payload = _canonical_view_json()
    payload["nodes"][1]["source"]["file"] = "rtl/counter.sv"

    cwd_path = tmp_path / "view.json"
    cwd_path.write_text(json.dumps(payload))

    import os

    prev = os.getcwd()
    os.chdir(tmp_path)
    try:
        resolver = resolver_from_paths(view_json_path=cwd_path)
        absolute = str(rel / "counter.sv")
        matches = resolver.src_to_view(file=absolute, line=22)
    finally:
        os.chdir(prev)
    assert "counter.u_ff" in matches


def test_src_to_view_empty_without_view_json():
    resolver = resolver_from_paths(view_json_path=None)
    assert resolver.src_to_view(file="/abs/rtl/x.sv", line=1) == ()


# ---------------------------------------------------------------------------
# signal → drivers
# ---------------------------------------------------------------------------


def test_signal_drivers_finds_unique_match(tmp_path: Path):
    resolver = resolver_from_paths(
        view_json_path=_write_view_json(tmp_path), tb_prefix="tb.dut."
    )
    drivers = resolver.signal_drivers(signal="q", wave_scope="tb.dut.")
    assert drivers == (SignalDriver(instance_path="counter.u_ff", port="q"),)


def test_signal_drivers_returns_all_drivers(tmp_path: Path):
    """Two instances port-connected to the same signal name collapse to a list."""

    payload = _sample_view_json()
    payload["nodes"].append(
        {
            "instance_path": "counter.u_ff2",
            "module_name": "counter_ff",
            "instance_name": "u_ff2",
            "port_connections": [
                {"port_name": "q", "net_expr_text": "q"},
            ],
            "location": {
                "file": "/abs/rtl/counter.sv",
                "start_line": 12,
                "start_column": 1,
                "end_line": 12,
                "end_column": 1,
            },
        }
    )
    payload["edges"].append({"parent": "counter", "child": "counter.u_ff2"})
    p = _write_view_json(tmp_path, payload)

    resolver = resolver_from_paths(view_json_path=p, tb_prefix="tb.dut.")
    drivers = resolver.signal_drivers(signal="q", wave_scope="tb.dut.")
    assert {d.instance_path for d in drivers} == {
        "counter.u_ff",
        "counter.u_ff2",
    }
    assert all(d.port == "q" for d in drivers)


def test_signal_drivers_unknown_signal_returns_empty(tmp_path: Path):
    resolver = resolver_from_paths(
        view_json_path=_write_view_json(tmp_path), tb_prefix="tb.dut."
    )
    assert resolver.signal_drivers(signal="ghost", wave_scope="tb.dut.") == ()


def test_signal_drivers_with_bad_wave_scope_returns_empty(tmp_path: Path):
    resolver = resolver_from_paths(
        view_json_path=_write_view_json(tmp_path), tb_prefix="tb.dut."
    )
    # Wave path that doesn't start with tb_prefix.
    assert resolver.signal_drivers(signal="q", wave_scope="other.foo") == ()


# ---------------------------------------------------------------------------
# lazy loading / mtime-driven reload
# ---------------------------------------------------------------------------


def test_resolver_returns_none_when_file_missing(tmp_path: Path):
    resolver = resolver_from_paths(
        view_json_path=tmp_path / "absent.json", tb_prefix="tb.dut."
    )
    # No view.json → view_to_wave falls through to best-effort path.
    # (This is the documented "no resolver loaded" fallback.)
    assert resolver.view_to_src("counter.u_ff") is None
    assert resolver.signal_drivers(signal="q", wave_scope="tb.dut.") == ()


def test_resolver_reloads_on_mtime_change(tmp_path: Path):
    p = _write_view_json(tmp_path)
    resolver = resolver_from_paths(view_json_path=p, tb_prefix="tb.dut.")
    assert resolver.view_to_src("counter.u_ff") is not None

    # Replace the file with a different top — bump mtime to force reload.
    new_payload = _sample_view_json(top="adder")
    p.write_text(json.dumps(new_payload), encoding="utf-8")
    import os, time

    os.utime(p, (time.time() + 1, time.time() + 1))

    assert resolver.view_to_src("counter.u_ff") is None
    assert resolver.view_to_src("adder.u_ff") is not None


def test_resolver_swallows_malformed_view_json(tmp_path: Path):
    p = tmp_path / "view.json"
    p.write_text("{not json", encoding="utf-8")
    resolver = resolver_from_paths(view_json_path=p, tb_prefix="tb.dut.")
    # Bad payload → resolver acts like view.json is absent.
    assert resolver.view_to_src("counter.u_ff") is None


def test_update_mapping_swaps_aliases_in_place(tmp_path: Path):
    p = _write_view_json(tmp_path)
    resolver = resolver_from_paths(
        view_json_path=p,
        tb_prefix="tb.dut.",
        signal_aliases=[SignalAlias(wave="old", view="counter.u_ff")],
    )
    assert resolver.wave_to_view("old") == "counter.u_ff"

    resolver.update_mapping(
        HubMappingConfig(
            tb_prefix="tb.dut.",
            signal_aliases=(SignalAlias(wave="new", view="counter.u_ff"),),
        )
    )
    assert resolver.wave_to_view("old") is None
    assert resolver.wave_to_view("new") == "counter.u_ff"


def test_default_view_json_path_layout(tmp_path: Path):
    assert default_view_json_path(tmp_path) == tmp_path / ".rtl-buddy" / "view.json"
