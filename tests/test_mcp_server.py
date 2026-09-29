"""Tests for ``rb mcp``, the stdio Model Context Protocol server.

The tests pin that the tool set is SDK-free, that stateless tools are always served while hub tools appear only when a live hub is discovered, that every result is the ``rb --machine`` payload in an envelope with ``rtl_buddy_version``, and that a bad question returns ``ok: false`` instead of raising. SDK-dependent tests skip when ``mcp`` is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.mcp import HUB_TOOL_NAMES, STATELESS_TOOL_NAMES, HubHandle, build_toolset
from rtl_buddy.mcp import server as mcp_server
from rtl_buddy.mcp import toolset as mcp_toolset
from rtl_buddy.rtl_buddy import RtlBuddy

_FIXTURES = Path(__file__).parent / "fixtures"

_HAS_SDK = mcp_server.sdk_available()
requires_sdk = pytest.mark.skipif(
    not _HAS_SDK, reason="the `mcp` SDK is not installed (optional extra)"
)


@pytest.fixture
def mcp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with a built (config-tier) graph and one result."""
    target = tmp_path / "project"
    shutil.copytree(_FIXTURES / "graph_config_tier", target)
    shutil.copy(_FIXTURES / "minimal_project" / "root_config.yaml", target)
    for name in ("blk_a", "blk_b"):
        (target / "design" / name / f"{name}.sv").write_text(
            f"module {name} (input logic clk);\nendmodule\n"
        )
    monkeypatch.chdir(target)

    rb = RtlBuddy(name="test_mcp_server")
    runner = CliRunner()
    built = runner.invoke(rb.app, ["graph", "build", "--no-design", "--no-extract"])
    assert built.exit_code == 0, built.output
    rb._artifact_locks.release_all()
    return target


@pytest.fixture
def empty_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with no graph built yet."""
    target = tmp_path / "bare"
    target.mkdir()
    monkeypatch.chdir(target)
    return target


def _totals(found: int, hit: int) -> dict:
    return {"found": found, "hit": hit, "ratio": None if not found else hit / found}


def _cov_totals(**metrics: tuple[int, int]) -> dict:
    return {
        metric: _totals(*metrics.get(metric, (0, 0)))
        for metric in ("line", "branch", "toggle", "expression", "cover")
    }


@pytest.fixture
def cov_project(mcp_project: Path) -> Path:
    """The graph project plus one run's hand-authored coverage artefacts."""
    from rtl_buddy.cov import manifest as manifest_mod
    from rtl_buddy.cov import model as model_mod

    cold_totals = _cov_totals(line=(3, 2), toggle=(1, 0))
    warm_totals = _cov_totals(line=(2, 2))
    model = {
        "schema_version": model_mod.MODEL_SCHEMA_VERSION,
        "generator": "rtl-buddy 0.0.0+test",
        "simulator": "verilator",
        "totals": _cov_totals(line=(5, 4), toggle=(1, 0)),
        "counts": {"files": 2, "tests": 1, "modules": 2},
        "modules": {
            "blk_a": ["design/blk_a/blk_a.sv"],
            "blk_b": ["design/blk_b/blk_b.sv"],
        },
        "tests": [
            {
                "name": "t_basic",
                "suite": "verif/blk_a/tests.yaml",
                "totals": _cov_totals(line=(5, 4), toggle=(1, 0)),
            }
        ],
        "files": [
            # Warm first, so a coldest-first answer must reorder.
            {
                "path": "design/blk_b/blk_b.sv",
                "modules": ["blk_b"],
                "totals": dict(warm_totals),
                "line": [
                    {"line": 1, "hits": 2, "tests": {"t_basic": 2}},
                    {"line": 2, "hits": 2, "tests": {"t_basic": 2}},
                ],
                "branch": [],
                "toggle": [],
                "expression": [],
                "cover": [],
            },
            {
                "path": "design/blk_a/blk_a.sv",
                "modules": ["blk_a"],
                "totals": dict(cold_totals),
                "line": [
                    {"line": 1, "hits": 4, "tests": {"t_basic": 4}},
                    {"line": 2, "hits": 1, "tests": {"t_basic": 1}},
                    {"line": 3, "hits": 0, "tests": {"t_basic": 0}},
                ],
                "branch": [],
                "toggle": [
                    {
                        "line": 1,
                        "column": 9,
                        "name": "q[0]",
                        "module": "blk_a",
                        "hits": 0,
                        "tests": {"t_basic": 0},
                    }
                ],
                "expression": [],
                "cover": [],
            },
        ],
    }
    cov_dir = mcp_project / "verif" / "blk_a" / "cov_dir"
    cov_dir.mkdir(parents=True)
    model_path = model_mod.write_model(model, cov_dir)
    manifest_mod.write_manifest(
        manifest_mod.build_manifest(
            project_root=mcp_project,
            cov_dir=cov_dir,
            command="regression",
            suite=mcp_project / "verif" / "blk_a" / "regression.yaml",
            builder="verilator",
            simulator_family="verilator",
            merge_mode="raw",
            model_path=model_path,
            totals=model["totals"],
            merged={"info": cov_dir / "coverage_merged.info"},
            tests=[{"name": "t_basic", "raw": cov_dir / "t_basic.dat"}],
        ),
        cov_dir,
    )
    return mcp_project


_PHYS_MODULES = [
    {"module": "blk_a", "cell_count": 120, "area_um2": 480.5},
    {"module": "sub", "cell_count": 40, "area_um2": 96.0},
]

_PHYS_INSTANCES = [
    {
        "instance_path": "u_sub/_64_",
        "module": "sub",
        "leakage_uw": 0.079,
        "internal_uw": 2.28,
        "switching_uw": 0.0675,
        "total_uw": 2.42,
    },
    {
        "instance_path": "u_sub/u_leaf/_12_",
        "module": "sub",
        "leakage_uw": 0.001,
        "internal_uw": 0.5,
        "switching_uw": 0.25,
        "total_uw": 0.751,
    },
]


@pytest.fixture
def phys_project(mcp_project: Path) -> Path:
    """The graph project plus one run's physical artefacts written by the phase-1 producers."""
    from rtl_buddy.phys.manifest import build_manifest, write_manifest
    from rtl_buddy.phys.model import (
        build_power_model,
        build_synth_model,
        merge_model,
        write_model,
    )

    phys_dir = mcp_project / "verif" / "blk_a" / "artefacts" / "nightly"
    phys_dir.mkdir(parents=True)
    # Both halves record the same netlist hash, as an `rb synth` then `rb power` pair does; without it neither inherits the other's rows (`rtl_buddy.phys.model.may_inherit_other_half`).
    netlist_sha256 = "0" * 64
    model = merge_model(
        build_synth_model(
            top="blk_a",
            modules=_PHYS_MODULES,
            area_um2=576.5,
            gate_count=160,
            netlist_sha256=netlist_sha256,
        ),
        build_power_model(
            top="blk_a",
            instances=_PHYS_INSTANCES,
            internal_w=2.78e-6,
            switching_w=0.3175e-6,
            leakage_w=0.08e-6,
            total_w=3.171e-6,
            netlist_sha256=netlist_sha256,
        ),
        own_half="instances",
    )
    model_path = write_model(model, phys_dir)
    write_manifest(
        build_manifest(
            project_root=mcp_project,
            phys_dir=phys_dir,
            command="power",
            run="nightly",
            top="blk_a",
            model_path=model_path,
            totals=model["totals"],
            synth={
                "backend": "yosys",
                "run": "nightly",
                "stats": phys_dir / "synth_stat.json",
                "netlist": phys_dir / "synth_netlist.v",
                "log": phys_dir / "synth.log",
            },
            power={
                "backend": "openroad",
                "run": "nightly",
                "netlist_source": "synth",
                "report": phys_dir / "power.rpt",
                "instances": phys_dir / "power_instances.rpt",
                "cells": phys_dir / "power_instances.cells",
                "log": phys_dir / "power.log",
            },
        ),
        phys_dir,
    )
    return mcp_project


def _toolset(project: Path, **kwargs):
    kwargs.setdefault("hub", HubHandle(present=False, reason="test: no hub"))
    return build_toolset(project, **kwargs)


def test_stateless_tools_are_served_without_a_hub(mcp_project: Path):
    """With no daemon, stateless tools give full answers."""
    ts = _toolset(mcp_project)

    assert ts.names() == list(STATELESS_TOOL_NAMES)
    assert not any(name in ts.names() for name in HUB_TOOL_NAMES)


def test_hub_tools_dial_in_when_a_hub_is_live(mcp_project: Path):
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))

    assert ts.names() == list(STATELESS_TOOL_NAMES) + list(HUB_TOOL_NAMES)


def test_a_stale_hub_record_does_not_advertise_hub_tools(mcp_project: Path):
    """A dead PID does not advertise hub tools."""
    hub_dir = mcp_project / ".rtl-buddy"
    hub_dir.mkdir(exist_ok=True)
    (hub_dir / "hub.json").write_text(
        json.dumps(
            {
                "v": 1,
                # A PID that cannot be live: the kernel never allocates 0.
                "pid": 0,
                "tcp": "127.0.0.1:9999",
                "server_version": "0.0.0",
                "project_root": str(mcp_project),
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )

    handle = mcp_toolset.discover_hub(mcp_project)

    assert handle.present is False
    assert "no live hub" in (handle.reason or "")


def test_coverage_reads_are_stateless_and_only_the_focus_needs_a_hub(
    mcp_project: Path,
):
    """A CI node answers coverage reads with no hub; only ``cov_focus`` needs one."""
    headless = _toolset(mcp_project)
    live = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))

    assert {"cov_summary", "cov_module"} <= set(headless.names())
    assert "cov_focus" not in headless.names()
    assert "cov_focus" in live.names()
    assert live.spec("cov_focus").command == "rb hub send cov-focus"
    assert headless.spec("cov_summary").command == "rb cov summary"
    assert headless.spec("cov_module").command == "rb cov module"


def test_every_schema_is_a_closed_object_with_resolvable_requireds(mcp_project: Path):
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))

    for spec in ts.specs():
        schema = spec.input_schema
        assert schema["type"] == "object", spec.name
        assert schema["additionalProperties"] is False, spec.name
        assert set(schema["required"]) <= set(schema["properties"]), spec.name
        assert spec.description and spec.title, spec.name


def test_a_result_is_the_machine_payload_plus_the_version(mcp_project: Path):
    """A result is the machine payload plus the version; neither surface has a shape the other lacks."""
    from rtl_buddy.graph.query import load_context, query as run_query

    ts = _toolset(mcp_project)
    envelope = ts.call("graph_query", {"question": "which tests cover A-COV-1"})

    assert envelope["ok"] is True
    assert envelope["meta"]["rtl_buddy_version"]
    assert envelope["meta"]["command"] == "rb graph query"

    direct = run_query(load_context(mcp_project), "which tests cover A-COV-1")
    assert envelope["payload"] == direct


def test_graph_status_reports_what_can_be_answered(mcp_project: Path):
    ts = _toolset(mcp_project)

    payload = ts.call("graph_status", {})["payload"]

    assert payload["graph_present"] is True
    assert payload["overlay_present"] is False
    assert payload["node_types"]["coverage_item"] == 4
    assert payload["hub"]["present"] is False
    assert payload["tools"] == list(STATELESS_TOOL_NAMES)


def test_graph_status_on_a_bare_checkout_points_at_graph_build(empty_project: Path):
    ts = _toolset(empty_project)

    payload = ts.call("graph_status", {})["payload"]

    assert payload["graph_present"] is False
    assert "rb graph build" in payload["hint"]


def test_path_and_explain_mirror_their_cli_verbs(mcp_project: Path):
    ts = _toolset(mcp_project)

    found = ts.call(
        "graph_path",
        {"source": "test:verif/blk_a#t_basic", "target": "covitem:blk_a#A-COV-1"},
    )
    explained = ts.call("graph_explain", {"node": "test:verif/blk_a#t_basic"})

    assert found["payload"]["length"] == 1
    assert explained["payload"]["degree"]["out"]["runs_on"] == 1


def test_graph_tools_are_lean_by_default_and_expand_on_request(mcp_project: Path):
    """Graph tools return lean edges and neighbours by default and full peer summaries only when 'expand' asks, as the CLI does."""
    ts = _toolset(mcp_project)

    lean = ts.call("graph_explain", {"node": "test:verif/blk_a#t_basic"})
    expanded = ts.call(
        "graph_explain", {"node": "test:verif/blk_a#t_basic", "expand": True}
    )

    assert all("node" not in e for e in lean["payload"]["outgoing"])
    assert all(e["peer_type"] for e in lean["payload"]["outgoing"])
    assert all(e["node"]["id"] == e["peer"] for e in expanded["payload"]["outgoing"])

    lean_q = ts.call("graph_query", {"question": "A-COV-1"})
    expanded_q = ts.call("graph_query", {"question": "A-COV-1", "expand": True})
    lean_neighbors = lean_q["payload"]["matches"][0]["neighbors"]
    expanded_neighbors = expanded_q["payload"]["matches"][0]["neighbors"]
    assert all("tier" not in n for n in lean_neighbors)
    assert all(n.get("tier") for n in expanded_neighbors)


def test_cov_summary_is_the_rb_cov_payload_verbatim(cov_project: Path):
    """``cov_summary`` returns the ``rb --machine cov summary`` payload."""
    from rtl_buddy.cov.query import load_context, summary_payload

    ts = _toolset(cov_project)
    envelope = ts.call("cov_summary", {})

    assert envelope["ok"] is True
    assert envelope["meta"]["command"] == "rb cov summary"
    assert envelope["payload"] == summary_payload(load_context(ts.project_root))
    assert envelope["payload"]["totals"]["line"]["hit"] == 4
    assert envelope["payload"]["modules"] == ["blk_a", "blk_b"]
    assert envelope["payload"]["artefacts"]["manifest"] == (
        "verif/blk_a/cov_dir/manifest.json"
    )


def test_cov_summary_truncates_coldest_first(cov_project: Path):
    """A limited summary keeps the coldest file."""
    ts = _toolset(cov_project)

    everything = ts.call("cov_summary", {"limit": 0})["payload"]["files"]
    coldest = ts.call("cov_summary", {"limit": 1})["payload"]["files"]

    assert [row["path"] for row in everything] == [
        "design/blk_a/blk_a.sv",
        "design/blk_b/blk_b.sv",
    ]
    assert [row["path"] for row in coldest] == ["design/blk_a/blk_a.sv"]
    # A summary file row carries totals only; points are cov_module's job.
    assert "line" not in coldest[0]


def test_cov_module_returns_the_points_and_the_tests_behind_them(cov_project: Path):
    from rtl_buddy.cov.query import load_context, module_payload

    ts = _toolset(cov_project)
    envelope = ts.call("cov_module", {"module": "blk_a"})

    assert envelope["ok"] is True
    assert envelope["payload"] == module_payload(load_context(ts.project_root), "blk_a")
    assert envelope["payload"]["tests"] == ["t_basic"]
    cold = [p for p in envelope["payload"]["files"][0]["toggle"] if not p["hits"]]
    assert [point["name"] for point in cold] == ["q[0]"]


def test_an_unknown_cov_module_returns_its_candidates(cov_project: Path):
    """An unknown cov module returns its near-miss candidates."""
    ts = _toolset(cov_project)

    envelope = ts.call("cov_module", {"module": "blk_z"})

    assert envelope["ok"] is False
    assert sorted(envelope["candidates"]) == ["blk_a", "blk_b"]


def test_cov_reads_a_named_cov_dir_instead_of_the_newest(cov_project: Path):
    ts = _toolset(cov_project)

    named = ts.call(
        "cov_summary", {"cov_dir": str(cov_project / "verif" / "blk_a" / "cov_dir")}
    )
    missing = ts.call("cov_summary", {"cov_dir": str(cov_project / "verif")})

    assert named["ok"] is True
    assert missing["ok"] is False
    assert "manifest.json" in missing["error"]


def test_cov_reads_a_relative_cov_dir_against_the_project_root(
    cov_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A relative ``cov_dir`` resolves against the project root, not the server's cwd.

    The host spawns ``rb mcp`` in a directory the agent never sees, so the repo-relative path from the payload (``artefacts.manifest``) is the natural argument.
    """
    ts = _toolset(cov_project)
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    relative = ts.call("cov_summary", {"cov_dir": "verif/blk_a/cov_dir"})
    absolute = ts.call(
        "cov_summary", {"cov_dir": str(cov_project / "verif" / "blk_a" / "cov_dir")}
    )

    assert relative["ok"] is True, relative.get("error")
    assert relative["payload"] == absolute["payload"]
    assert relative["payload"]["artefacts"]["manifest"] == (
        "verif/blk_a/cov_dir/manifest.json"
    )


def test_cov_reads_a_relative_manifest_against_the_project_root(
    cov_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A relative ``manifest`` resolves against the project root; it is the path the summary reports."""
    ts = _toolset(cov_project)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    envelope = ts.call(
        "cov_module",
        {"module": "blk_a", "manifest": "verif/blk_a/cov_dir/manifest.json"},
    )

    assert envelope["ok"] is True, envelope.get("error")
    assert envelope["payload"]["tests"] == ["t_basic"]


# Coverage tools that take the discovery overrides, with their other arguments; the guarantees below belong to the shared helper and are asserted for each.
_COV_TOOL_CALLS = (
    ("cov_summary", {}),
    ("cov_module", {"module": "blk_a"}),
)


@pytest.mark.parametrize(("tool", "args"), _COV_TOOL_CALLS)
def test_a_cov_path_override_that_is_not_a_string_is_refused_as_a_tool_error(
    cov_project: Path, tool: str, args: dict
):
    """A cov path override that is not a string is refused with an ``ok: false`` tool error.

    A host's arguments are not checked against ``inputSchema``, so ``cov_dir`` can be a number; ``Path()`` would raise a ``TypeError`` that ``Toolset.call`` does not catch.
    """
    ts = _toolset(cov_project)

    for key, bad, shown in (
        ("cov_dir", 3, "3"),
        ("cov_dir", [], "[]"),
        ("cov_dir", True, "True"),
        ("manifest", [], "[]"),
        ("manifest", {"path": "x"}, "{'path': 'x'}"),
    ):
        refused = ts.call(tool, dict(args, **{key: bad}))

        assert refused["ok"] is False, (tool, key, bad)
        # The unreadable value is quoted back.
        assert f"{key} must be a path string, not {shown}" in refused["error"]
        assert "omit it to read the newest run" in refused["error"]
        assert "payload" not in refused

    # A string path still answers.
    answered = ts.call(tool, dict(args, cov_dir="verif/blk_a/cov_dir"))
    assert answered["ok"] is True, answered.get("error")


@pytest.mark.parametrize(("tool", "args"), _COV_TOOL_CALLS)
def test_an_absent_cov_path_override_still_means_discover_the_newest_run(
    cov_project: Path, tool: str, args: dict
):
    """``None`` for an override, including an explicit ``null``, means discover the newest run."""
    ts = _toolset(cov_project)

    assert ts.call(tool, dict(args))["ok"] is True
    assert ts.call(tool, dict(args, cov_dir=None, manifest=None))["ok"] is True


@pytest.mark.parametrize(
    ("bad", "shown"),
    (
        (None, "None"),
        ("ten", "'ten'"),
        ([], "[]"),
        (False, "False"),
        (True, "True"),
        (2.7, "2.7"),
        (-0.5, "-0.5"),
    ),
)
def test_a_cov_summary_limit_that_is_not_a_whole_count_is_refused(
    cov_project: Path, bad, shown: str
):
    """A cov summary ``limit`` that is not a whole count is refused.

    ``int()`` would turn ``false`` into 0 (every file), truncate ``2.7``, and raise on ``null``, ``"ten"`` or ``[]`` past ``Toolset.call``.
    """
    ts = _toolset(cov_project)

    refused = ts.call("cov_summary", {"limit": bad})

    assert refused["ok"] is False
    # The unreadable value is quoted back.
    assert f"limit must be an integer, not {shown}" in refused["error"]
    assert "0 lists every row" in refused["error"]
    assert "payload" not in refused


def test_a_negative_cov_summary_limit_is_refused_rather_than_read_as_all(
    cov_project: Path,
):
    """A negative cov summary ``limit`` is refused, not read as all files.

    ``coldest_first`` treats anything ``<= 0`` as no cap.
    """
    ts = _toolset(cov_project)

    refused = ts.call("cov_summary", {"limit": -1})

    assert refused["ok"] is False
    assert "limit must be 0 or greater, not -1" in refused["error"]
    assert "0 lists every row" in refused["error"]
    assert "payload" not in refused

    # 0 is untouched; it asks for all files.
    everything = ts.call("cov_summary", {"limit": 0})
    assert everything["ok"] is True
    assert len(everything["payload"]["files"]) == 2


def test_a_whole_cov_summary_limit_still_heads_the_file_list(cov_project: Path):
    """A whole-number ``limit`` still heads the file list. ``1.0`` and a decimal string are accepted, and an absent ``limit`` uses the CLI default."""
    from rtl_buddy.cov.query import DEFAULT_FILE_LIMIT

    ts = _toolset(cov_project)

    for good in (1, 1.0, "1"):
        answered = ts.call("cov_summary", {"limit": good})
        assert answered["ok"] is True, good
        assert len(answered["payload"]["files"]) == 1, good

    # Absent means the documented default, which this run is smaller than.
    default = ts.call("cov_summary", {})
    assert default["ok"] is True
    assert (
        default["payload"]
        == ts.call("cov_summary", {"limit": DEFAULT_FILE_LIMIT})["payload"]
    )


def test_a_project_with_no_coverage_run_names_the_command_that_makes_one(
    empty_project: Path,
):
    ts = _toolset(empty_project)

    envelope = ts.call("cov_summary", {})

    assert envelope["ok"] is False
    assert "--coverage-merge" in envelope["error"]


def test_cov_focus_omits_the_hints_it_was_not_given(
    mcp_project: Path, monkeypatch: pytest.MonkeyPatch
):
    """``cov_focus`` omits hints it was not given (``additionalProperties: false``, no nullable hints)."""
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))
    sent: dict = {}
    monkeypatch.setattr(
        ts,
        "_hub_emit",
        lambda type_, payload: sent.update({"type": type_, "payload": payload}) or {},
    )

    ts.call("cov_focus", {"target": "module:blk_a", "metric": "toggle"})

    assert sent["type"] == "cov_focus"
    assert sent["payload"] == {"target": "module:blk_a", "metric": "toggle"}


def test_cov_focus_puts_the_same_bytes_on_the_wire_as_its_cli_verb(
    mcp_project: Path, monkeypatch: pytest.MonkeyPatch
):
    """Padded input gives one payload from the MCP tool and ``rb hub send``.

    The pane matches ``target`` and ``item`` as strings, so both surfaces validate and emit the stripped value.
    """
    from rtl_buddy.hub import send as hub_send

    padded = {"target": "  module:blk_a  ", "metric": "toggle", "item": "  q[0] \t"}

    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))
    from_mcp: dict = {}
    monkeypatch.setattr(
        ts,
        "_hub_emit",
        lambda type_, payload: (
            from_mcp.update({"type": type_, "payload": payload}) or {}
        ),
    )
    ts.call("cov_focus", dict(padded))

    from_cli: dict = {}

    class _Recorder:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def emit(self, type_, payload):
            from_cli.update({"type": type_, "payload": payload})

    monkeypatch.setattr(hub_send, "_open_or_exit", _Recorder)
    hub_send.cmd_cov_focus(
        padded["target"], metric=padded["metric"], item=padded["item"]
    )

    assert from_mcp == from_cli
    assert from_cli == {
        "type": "cov_focus",
        "payload": {"target": "module:blk_a", "metric": "toggle", "item": "q[0]"},
    }


def test_cov_focus_validates_before_dialling(mcp_project: Path):
    """Port 1 refuses connections; reaching it would mean no validation."""
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:1"))

    envelope = ts.call("cov_focus", {"target": "design/blk_a.sv", "metric": "lines"})

    assert envelope["ok"] is False
    assert "metric" in envelope["error"]


def test_physical_reads_are_stateless_and_mirror_their_cli_verbs(mcp_project: Path):
    """A CI node answers phys reads with no hub, and no EDA tool is run."""
    headless = _toolset(mcp_project)

    assert {"phys_summary", "phys_module", "phys_instance"} <= set(headless.names())
    assert headless.spec("phys_summary").command == "rb phys summary"
    assert headless.spec("phys_module").command == "rb phys module"
    assert headless.spec("phys_instance").command == "rb phys instance"


def test_phys_module_does_not_claim_it_answers_a_flat_netlists_top(
    mcp_project: Path,
):
    """``phys_module`` does not claim to answer a flat netlist's top: no leaf row carries an RTL module's name."""
    description = _toolset(mcp_project).spec("phys_module").description

    assert "flat netlist's top" not in description
    assert "Liberty-cell questions ('how much do the DFFs burn') and " in description
    assert "flattening the design changes the hierarchy rather than the " in description


def test_phys_module_scopes_the_liberty_only_claim_to_the_power(
    mcp_project: Path,
):
    """``phys_module`` limits the Liberty-only claim to the power attribution; the synthesis half measures RTL modules."""
    description = _toolset(mcp_project).spec("phys_module").description

    assert "and nothing else" not in description
    assert "still gets its synthesis row" in description
    assert "the POWER is attributed by that join" in description
    # The empty instance list is not evidence against the row above it.
    assert "do not read it back onto the cells and area, which stand" in description


def test_the_phys_tools_say_which_instance_targets_are_focusable(
    mcp_project: Path,
):
    """The phys tool descriptions say which instance targets are focusable; the pane resolves exact leaf rows, so a subtree path soft-misses."""
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))
    instance = ts.spec("phys_instance").description
    focus = ts.spec("phys_focus").description

    assert "'match' says how the path landed" in instance
    assert "only an exact row is a row" in instance
    assert "never a subtree prefix" in instance

    assert "the pane resolves exact leaf rows only" in focus
    assert "focusable when its 'match' is 'exact'" in focus
    assert "focus one of the 'children' instead" in focus


def test_the_phys_detail_tools_head_their_lists_by_default(phys_project: Path):
    """The phys detail tools head their lists by default.

    The payload carries the applied limit, the untruncated count and sums over every row; an agent wanting all rows says so.
    """
    from rtl_buddy.phys.query import DEFAULT_RANK_LIMIT

    ts = _toolset(phys_project)

    for tool, args, listed, counted in (
        ("phys_module", {"module": "sub"}, "instances", "instance_count"),
        ("phys_instance", {"path": "u_sub"}, "children", "child_count"),
    ):
        default = ts.call(tool, dict(args))["payload"]
        headed = ts.call(tool, dict(args, limit=1))["payload"]
        every = ts.call(tool, dict(args, limit=0))["payload"]

        assert default["limit"] == DEFAULT_RANK_LIMIT
        assert default[listed] == every[listed]  # 2 rows, well under the cap
        assert len(headed[listed]) == 1
        # Self-describing: the count and the sum cover every matching row.
        assert headed[counted] == 2
        assert headed["limit"] == 1

    # The sums do not shrink with the list.
    hot = ts.call("phys_instance", {"path": "u_sub", "limit": 1})["payload"]
    assert hot["rollup"]["instances"] == 2
    assert hot["rollup"]["total_uw"] == pytest.approx(3.171)
    assert ts.call("phys_module", {"module": "sub", "limit": 1})["payload"]["power"][
        "total_uw"
    ] == pytest.approx(3.171)


def test_a_negative_phys_limit_is_refused_rather_than_read_as_all(
    phys_project: Path,
):
    """A negative phys ``limit`` is refused, not read as all rows.

    The server forwards arguments unchecked, and the shared ``truncate`` treats anything ``<= 0`` as no head.
    """
    ts = _toolset(phys_project)

    for tool, args in (
        ("phys_summary", {}),
        ("phys_module", {"module": "sub"}),
        ("phys_instance", {"path": "u_sub"}),
    ):
        refused = ts.call(tool, dict(args, limit=-1))

        assert refused["ok"] is False, tool
        # The message names the constraint so an agent can fix the call.
        assert "limit must be 0 or greater, not -1" in refused["error"]
        assert "0 lists every row" in refused["error"]
        # A refusal is not a payload.
        assert "payload" not in refused

    # 0 is untouched; it asks for all rows.
    assert ts.call("phys_module", {"module": "sub", "limit": 0})["ok"] is True


def test_a_phys_limit_that_is_not_an_integer_is_refused_before_it_is_coerced(
    phys_project: Path,
):
    """A phys ``limit`` that is not an integer is refused before coercion.

    ``int()`` turns ``false`` into 0 (every row) and truncates ``-0.5`` to 0.
    """
    ts = _toolset(phys_project)

    for bad, shown in (
        (False, "False"),
        (True, "True"),
        (2.7, "2.7"),
        (-0.5, "-0.5"),
    ):
        refused = ts.call("phys_module", {"module": "sub", "limit": bad})

        assert refused["ok"] is False, bad
        assert f"limit must be an integer, not {shown}" in refused["error"]
        assert "0 lists every row" in refused["error"]
        assert "payload" not in refused

    # Integral floats still answer: `2.0` is how some hosts spell `2`, and a decimal string converts.
    for good, expected in ((2.0, 2), (0.0, 0), ("1", 1)):
        answered = ts.call("phys_module", {"module": "sub", "limit": good})
        assert answered["ok"] is True, good
        assert answered["payload"]["limit"] == expected

    # The shared helper enforces this for every physical tool that takes a limit.
    for tool, args in (
        ("phys_summary", {}),
        ("phys_module", {"module": "sub"}),
        ("phys_instance", {"path": "u_sub"}),
    ):
        assert ts.call(tool, dict(args, limit=False))["ok"] is False, tool


def test_a_phys_limit_that_is_not_a_number_is_refused_as_a_tool_error(
    phys_project: Path,
):
    """A phys ``limit`` that is ``null``, a word or a list is refused with an ``ok: false`` tool error, not a protocol-level failure."""
    ts = _toolset(phys_project)

    for bad, shown in ((None, "None"), ("ten", "'ten'"), ([], "[]")):
        refused = ts.call("phys_module", {"module": "sub", "limit": bad})

        assert refused["ok"] is False, bad
        # The unreadable value is quoted back.
        assert f"limit must be an integer, not {shown}" in refused["error"]
        assert "0 lists every row" in refused["error"]
        assert "payload" not in refused

    # The shared helper enforces this for every physical tool that takes a limit.
    for tool, args in (
        ("phys_summary", {}),
        ("phys_module", {"module": "sub"}),
        ("phys_instance", {"path": "u_sub"}),
    ):
        assert ts.call(tool, dict(args, limit=None))["ok"] is False, tool

    # A number that arrived as a string is still a number.
    assert ts.call("phys_module", {"module": "sub", "limit": "1"})["ok"] is True


def test_a_phys_path_override_that_is_not_a_string_is_refused_as_a_tool_error(
    phys_project: Path,
):
    """A phys path override that is not a string is refused with an ``ok: false`` tool error.

    `phys_dir` can arrive as a number and `manifest` as a list; `Path()` would raise a `TypeError` that `Toolset.call` does not catch.
    """
    ts = _toolset(phys_project)

    for key, bad, shown in (
        ("phys_dir", 3, "3"),
        ("phys_dir", [], "[]"),
        ("manifest", [], "[]"),
        ("manifest", {"path": "x"}, "{'path': 'x'}"),
        ("phys_dir", True, "True"),
    ):
        refused = ts.call("phys_module", {"module": "sub", key: bad})

        assert refused["ok"] is False, (key, bad)
        # The unreadable value is quoted back.
        assert f"{key} must be a path string, not {shown}" in refused["error"]
        assert "omit it to read the newest run" in refused["error"]
        assert "payload" not in refused

    # The shared helper enforces this for every physical tool that takes the overrides.
    for tool, args in (
        ("phys_summary", {}),
        ("phys_module", {"module": "sub"}),
        ("phys_instance", {"path": "u_sub"}),
    ):
        assert ts.call(tool, dict(args, phys_dir=3))["ok"] is False, tool
        assert ts.call(tool, dict(args, manifest=[]))["ok"] is False, tool


def test_an_absent_phys_path_override_still_means_discover_the_newest_run(
    phys_project: Path,
):
    """``None`` for an override, including an explicit `null`, means discover the newest run."""
    ts = _toolset(phys_project)

    assert ts.call("phys_summary", {})["ok"] is True
    assert ts.call("phys_summary", {"phys_dir": None, "manifest": None})["ok"] is True


def test_the_phys_detail_tools_declare_their_limit_like_the_cli(mcp_project: Path):
    """The phys detail tools declare in the schema that the default is a head and 0 means all."""
    from rtl_buddy.phys.query import DEFAULT_RANK_LIMIT

    ts = _toolset(mcp_project)

    for tool, listed in (
        ("phys_module", "Instance rows to list"),
        ("phys_instance", "Child rows to list"),
    ):
        schema = ts.spec(tool).input_schema
        limit = schema["properties"]["limit"]
        assert limit["type"] == "integer"
        assert limit["minimum"] == 0
        assert limit["description"].startswith(listed)
        assert f"default {DEFAULT_RANK_LIMIT}; 0 for all" in limit["description"]
        # Not required: the default is the point.
        assert "limit" not in schema.get("required", [])
        assert f"(default {DEFAULT_RANK_LIMIT}, 0 for all)" in ts.spec(tool).description


def test_phys_summary_takes_a_limit_per_ranking(phys_project: Path):
    """``phys_summary`` takes a limit per ranking.

    ``limit`` heads both rankings, each override wins for its own ranking, and ``"none"`` suppresses one because ``0`` means all.
    """
    from rtl_buddy.phys.query import DEFAULT_RANK_LIMIT

    ts = _toolset(phys_project)

    modules_only = ts.call("phys_summary", {"limit": 0, "instances_limit": "none"})[
        "payload"
    ]
    assert len(modules_only["modules"]) == 2
    assert modules_only["instances"] == []
    assert modules_only["limits"] == {"modules": 0, "instances": "none"}
    # The counts are the model's rows, not the listed ones, so a suppressed ranking is not an empty half.
    assert modules_only["counts"] == {"modules": 2, "instances": 2}

    headed = ts.call("phys_summary", {"limit": 0, "modules_limit": 1})["payload"]
    assert len(headed["modules"]) == 1
    assert len(headed["instances"]) == 2
    assert headed["limits"] == {"modules": 1, "instances": 0}

    # Neither override: the payload an existing host already gets.
    default = ts.call("phys_summary", {})["payload"]
    assert default["limit"] == DEFAULT_RANK_LIMIT
    assert default["limits"] == {
        "modules": DEFAULT_RANK_LIMIT,
        "instances": DEFAULT_RANK_LIMIT,
    }


def test_a_per_ranking_phys_summary_limit_is_validated_like_the_shared_one(
    phys_project: Path,
):
    """The per-ranking overrides use the same validation as ``limit``: negatives and bools are refused, and a word other than ``"none"`` is refused naming its input."""
    ts = _toolset(phys_project)

    for key in ("modules_limit", "instances_limit"):
        refused = ts.call("phys_summary", {key: -1})
        assert refused["ok"] is False, key
        assert f"{key} must be 0 or greater, not -1" in refused["error"]
        assert "payload" not in refused

        for bad, shown in ((False, "False"), ("all", "'all'"), (None, "None")):
            refused = ts.call("phys_summary", {key: bad})
            assert refused["ok"] is False, (key, bad)
            assert f"{key} must be an integer, not {shown}" in refused["error"]

        # The word, in any case, is the one non-number accepted.
        assert ts.call("phys_summary", {key: "NONE"})["ok"] is True
        # A number that arrived as a string is still a number.
        assert (
            ts.call("phys_summary", {key: "1"})["payload"]["limits"][
                key.removesuffix("_limit")
            ]
            == 1
        )


def test_phys_summary_declares_its_per_ranking_limits(mcp_project: Path):
    """The ``phys_summary`` schema says ``"none"`` is legal and that omitting an override follows ``limit``."""
    ts = _toolset(mcp_project)

    schema = ts.spec("phys_summary").input_schema
    for key, listed in (
        ("modules_limit", "Module rows"),
        ("instances_limit", "Instance rows"),
    ):
        prop = schema["properties"][key]
        assert prop["anyOf"] == [
            {"type": "integer", "minimum": 0},
            {"type": "string", "enum": ["none"]},
        ]
        assert prop["description"].startswith(listed)
        assert "'none' for no rows at all" in prop["description"]
        assert "omit to follow 'limit'" in prop["description"]
        assert key not in schema.get("required", [])
    assert (
        "take 'none' for a ranking you do not want"
        in ts.spec("phys_summary").description
    )


def test_phys_runs_is_the_rb_phys_runs_payload_verbatim(phys_project: Path):
    """``phys_runs`` returns the ``rb --machine phys runs`` payload, the menu that supplies ``phys_dir`` to the other tools."""
    from rtl_buddy.phys.query import DEFAULT_RUNS_LIMIT, runs_payload

    ts = _toolset(phys_project)
    envelope = ts.call("phys_runs", {})

    assert envelope["ok"] is True
    assert envelope["meta"]["command"] == "rb phys runs"
    assert envelope["payload"] == runs_payload(
        ts.project_root, limit=DEFAULT_RUNS_LIMIT
    )
    entry = envelope["payload"]["runs"][0]
    assert entry["phys_dir"] == "verif/blk_a/artefacts/nightly"
    assert entry["newest"] is True
    # The directory it names is one `phys_summary` accepts.
    answered = ts.call("phys_summary", {"phys_dir": entry["phys_dir"]})
    assert answered["payload"]["run"] == entry["run"]


def test_phys_runs_needs_no_hub_and_no_arguments(phys_project: Path):
    """``phys_runs`` needs no hub and no arguments, so an agent can call it first."""
    from rtl_buddy.mcp.toolset import STATELESS_TOOL_NAMES

    assert "phys_runs" in STATELESS_TOOL_NAMES
    ts = _toolset(phys_project)
    assert ts.spec("phys_runs").input_schema.get("required", []) == []
    assert ts.call("phys_runs", {"limit": 1})["payload"]["limit"] == 1


def test_phys_runs_validates_its_limit_like_every_other_physical_tool(
    phys_project: Path,
):
    """``phys_runs`` validates its ``limit`` through the shared helper; ``-1`` must not return every run."""
    from rtl_buddy.phys.query import DEFAULT_RUNS_LIMIT, runs_payload

    ts = _toolset(phys_project)

    refused = ts.call("phys_runs", {"limit": -1})
    assert refused["ok"] is False
    assert "limit must be 0 or greater, not -1" in refused["error"]
    assert "payload" not in refused

    # A non-numeric limit gets the same refusal.
    assert ts.call("phys_runs", {"limit": "ten"})["ok"] is False

    # The run listing keeps its own default, DEFAULT_RUNS_LIMIT.
    assert ts.call("phys_runs", {})["payload"] == runs_payload(
        ts.project_root, limit=DEFAULT_RUNS_LIMIT
    )
    # 0 still means all.
    assert ts.call("phys_runs", {"limit": 0})["payload"]["limit"] == 0


def test_phys_summary_is_the_rb_phys_payload_verbatim(phys_project: Path):
    """``phys_summary`` returns the ``rb --machine phys summary`` payload."""
    from rtl_buddy.phys.query import load_context, summary_payload

    ts = _toolset(phys_project)
    envelope = ts.call("phys_summary", {})

    assert envelope["ok"] is True
    assert envelope["meta"]["command"] == "rb phys summary"
    assert envelope["payload"] == summary_payload(load_context(ts.project_root))
    assert set(envelope["payload"]) == {
        "schema_version",
        "manifest",
        "model",
        "generated_at",
        "run_command",
        "run",
        "top",
        "backends",
        # The run's identity, beside where it is.
        "power_mode",
        "power_activity",
        "config",
        "xplr",
        "units",
        "totals",
        "counts",
        "halves",
        "missing_halves",
        "limit",
        # What each ranking was actually headed at.
        "limits",
        "modules",
        "instances",
        "artefacts",
    }
    assert envelope["payload"]["missing_halves"] == []
    assert [row["module"] for row in envelope["payload"]["modules"]] == ["blk_a", "sub"]
    assert envelope["payload"]["artefacts"]["manifest"] == (
        "verif/blk_a/artefacts/nightly/phys-manifest.json"
    )


def test_phys_summary_truncates_the_rankings_heaviest_first(phys_project: Path):
    """A limited summary keeps the heaviest row."""
    ts = _toolset(phys_project)

    everything = ts.call("phys_summary", {"limit": 0})["payload"]
    heaviest = ts.call("phys_summary", {"limit": 1})["payload"]

    assert [row["module"] for row in everything["modules"]] == ["blk_a", "sub"]
    assert [row["module"] for row in heaviest["modules"]] == ["blk_a"]
    assert [row["instance_path"] for row in heaviest["instances"]] == ["u_sub/_64_"]


def test_phys_module_joins_the_synthesis_row_to_the_instances_of_it(
    phys_project: Path,
):
    from rtl_buddy.phys.query import load_context, module_payload

    ts = _toolset(phys_project)
    envelope = ts.call("phys_module", {"module": "sub", "limit": 0})

    assert envelope["ok"] is True
    assert envelope["payload"] == module_payload(
        load_context(ts.project_root), "sub", limit=0
    )
    assert envelope["payload"]["row"]["cell_count"] == 40
    assert envelope["payload"]["instance_count"] == 2
    # `limit: 0` is the complete list, as in the CLI verb, from the same builder.
    assert envelope["payload"]["limit"] == 0
    assert len(envelope["payload"]["instances"]) == 2
    assert envelope["payload"]["power"]["total_uw"] == pytest.approx(3.171)


def test_phys_instance_rolls_up_the_subtree_under_a_path(phys_project: Path):
    from rtl_buddy.phys.query import instance_payload, load_context

    ts = _toolset(phys_project)
    envelope = ts.call("phys_instance", {"path": "u_sub", "limit": 0})

    assert envelope["ok"] is True
    assert envelope["payload"] == instance_payload(
        load_context(ts.project_root), "u_sub", limit=0
    )
    assert envelope["payload"]["match"] == "prefix"
    assert envelope["payload"]["rollup"]["instances"] == 2
    assert envelope["payload"]["rollup"]["total_uw"] == pytest.approx(3.171)
    # Complete, as `phys_module`'s passthrough is.
    assert envelope["payload"]["limit"] == 0
    assert len(envelope["payload"]["children"]) == envelope["payload"]["child_count"]


def test_an_unknown_phys_module_returns_its_candidates(phys_project: Path):
    """An unknown phys module returns its near-miss candidates."""
    ts = _toolset(phys_project)

    envelope = ts.call("phys_module", {"module": "blk_z"})

    assert envelope["ok"] is False
    assert envelope["candidates"] == ["blk_a"]


def test_an_unknown_phys_instance_returns_its_candidates(phys_project: Path):
    ts = _toolset(phys_project)

    envelope = ts.call("phys_instance", {"path": "u_nope"})

    assert envelope["ok"] is False
    assert envelope["candidates"] == ["u_sub/_64_", "u_sub/u_leaf/_12_"]


def test_phys_reads_a_named_phys_dir_instead_of_the_newest(phys_project: Path):
    ts = _toolset(phys_project)
    nightly = phys_project / "verif" / "blk_a" / "artefacts" / "nightly"

    named = ts.call("phys_summary", {"phys_dir": str(nightly)})
    missing = ts.call("phys_summary", {"phys_dir": str(phys_project / "verif")})

    assert named["ok"] is True
    assert missing["ok"] is False
    assert "phys-manifest.json" in missing["error"]


def test_phys_reads_a_relative_phys_dir_against_the_project_root(
    phys_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A relative ``phys_dir`` resolves against the project root, not the server's cwd.

    The host spawns ``rb mcp`` in a directory the agent never sees, so the repo-relative path from the payload (``artefacts.manifest``) is the natural argument.
    """
    ts = _toolset(phys_project)
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    relative = ts.call("phys_summary", {"phys_dir": "verif/blk_a/artefacts/nightly"})
    absolute = ts.call(
        "phys_summary",
        {"phys_dir": str(phys_project / "verif" / "blk_a" / "artefacts" / "nightly")},
    )

    assert relative["ok"] is True, relative.get("error")
    assert relative["payload"] == absolute["payload"]
    assert relative["payload"]["artefacts"]["manifest"] == (
        "verif/blk_a/artefacts/nightly/phys-manifest.json"
    )


def test_phys_reads_a_relative_manifest_against_the_project_root(
    phys_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A relative ``manifest`` resolves against the project root; it is the path the summary reports."""
    ts = _toolset(phys_project)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    envelope = ts.call(
        "phys_module",
        {
            "module": "sub",
            "manifest": "verif/blk_a/artefacts/nightly/phys-manifest.json",
        },
    )

    assert envelope["ok"] is True, envelope.get("error")
    assert envelope["payload"]["instance_count"] == 2


def test_a_project_with_no_physical_run_names_the_commands_that_make_one(
    empty_project: Path,
):
    ts = _toolset(empty_project)

    envelope = ts.call("phys_summary", {})

    assert envelope["ok"] is False
    assert "rb synth" in envelope["error"]
    assert "rb power" in envelope["error"]


def test_phys_focus_needs_a_hub_and_the_reads_do_not(mcp_project: Path):
    """``phys_focus`` needs a hub; the three reads answer from disk."""
    headless = _toolset(mcp_project)
    live = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))

    assert "phys_focus" not in headless.names()
    assert "phys_focus" in live.names()
    assert live.spec("phys_focus").command == "rb hub send phys-focus"


def test_phys_focus_omits_the_metric_it_was_not_given(
    mcp_project: Path, monkeypatch: pytest.MonkeyPatch
):
    """``phys_focus`` omits the metric it was not given (``additionalProperties: false``, no nullable hints)."""
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))
    sent: dict = {}
    monkeypatch.setattr(
        ts,
        "_hub_emit",
        lambda type_, payload: sent.update({"type": type_, "payload": payload}) or {},
    )

    ts.call("phys_focus", {"target": "module:sub"})

    assert sent["type"] == "phys_focus"
    assert sent["payload"] == {"target": "module:sub"}


def test_phys_focus_puts_the_same_bytes_on_the_wire_as_its_cli_verb(
    mcp_project: Path, monkeypatch: pytest.MonkeyPatch
):
    """Padded input gives one payload from the MCP tool and ``rb hub send``.

    The pane matches ``target`` as a string, so both surfaces validate and emit the stripped value.
    """
    from rtl_buddy.hub import send as hub_send

    padded = {"target": "  instance:u_sub/_64_  ", "metric": "dynamic"}

    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))
    from_mcp: dict = {}
    monkeypatch.setattr(
        ts,
        "_hub_emit",
        lambda type_, payload: (
            from_mcp.update({"type": type_, "payload": payload}) or {}
        ),
    )
    ts.call("phys_focus", dict(padded))

    from_cli: dict = {}

    class _Recorder:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def emit(self, type_, payload):
            from_cli.update({"type": type_, "payload": payload})

    monkeypatch.setattr(hub_send, "_open_or_exit", _Recorder)
    hub_send.cmd_phys_focus(padded["target"], metric=padded["metric"])

    assert from_mcp == from_cli
    assert from_cli == {
        "type": "phys_focus",
        "payload": {"target": "instance:u_sub/_64_", "metric": "dynamic"},
    }


def test_phys_focus_validates_before_dialling(mcp_project: Path):
    """``phys_focus`` validates before dialling.

    Port 1 refuses connections, so reaching it would mean no validation. ``switching`` is a model column but not a pane metric; the enum is the hub's wire schema.
    """
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:1"))

    envelope = ts.call("phys_focus", {"target": "module:sub", "metric": "switching"})

    assert envelope["ok"] is False
    assert "metric" in envelope["error"]
    assert "cells/area/leakage/dynamic/total" in envelope["error"]


def test_a_focus_target_that_is_not_a_string_is_refused_before_the_hub(
    mcp_project: Path,
):
    """A focus target that is not a string is refused before the hub.

    ``str()`` would render ``false`` as ``"False"`` and ``[]`` as ``"[]"``, and the hub would cache and replay it to every pane that registers. Port 1 refuses connections, so reaching it would mean no validation.
    """
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:1"))

    for bad, shown in (
        (False, "False"),
        (True, "True"),
        ([], "[]"),
        ({}, "{}"),
        (3, "3"),
    ):
        refused = ts.call("phys_focus", {"target": bad})

        assert refused["ok"] is False, bad
        # The unreadable value is quoted back.
        assert "'target' must be a string" in refused["error"], bad
        assert f"not {shown}" in refused["error"], bad
        assert "instance path" in refused["error"]

    # The shared helper enforces this for the coverage pane's focus verb too.
    assert ts.call("cov_focus", {"target": []})["ok"] is False

    # An absent or blank target is still the missing-argument error.
    for blank in (None, "", "   "):
        refused = ts.call("phys_focus", {"target": blank})
        assert refused["ok"] is False, blank
        assert "missing required argument 'target'" in refused["error"], blank


def test_a_phys_focus_metric_that_is_not_a_pane_metric_is_refused(mcp_project: Path):
    """A phys focus metric outside the five pane metrics is refused, whatever its type."""
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:1"))

    for bad in (False, [], {}, 0, "switching"):
        refused = ts.call("phys_focus", {"target": "module:sub", "metric": bad})

        assert refused["ok"] is False, bad
        assert "metric" in refused["error"], bad
        assert "cells/area/leakage/dynamic/total" in refused["error"], bad


def test_phys_focus_reports_a_dead_hub_rather_than_crashing(mcp_project: Path):
    """The handle said yes at start; the socket may still refuse."""
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:1"))

    envelope = ts.call("phys_focus", {"target": "module:sub"})

    assert envelope["ok"] is False
    assert "hub" in envelope["error"].lower()
    assert envelope["meta"]["command"] == "rb hub send phys-focus"


def test_an_unknown_tool_is_an_answer(mcp_project: Path):
    ts = _toolset(mcp_project)

    envelope = ts.call("no_such_tool", {})

    assert envelope["ok"] is False
    assert "unknown tool" in envelope["error"]
    assert "graph_query" in envelope["error"]


def test_a_missing_argument_is_an_answer(mcp_project: Path):
    ts = _toolset(mcp_project)

    envelope = ts.call("graph_query", {})

    assert envelope["ok"] is False
    assert "question" in envelope["error"]


def test_a_missing_graph_is_an_answer_naming_the_build_command(empty_project: Path):
    ts = _toolset(empty_project)

    envelope = ts.call("graph_query", {"question": "anything"})

    assert envelope["ok"] is False
    assert "rb graph build" in envelope["error"]


def test_an_ambiguous_node_returns_its_candidates(mcp_project: Path):
    ts = _toolset(mcp_project)

    envelope = ts.call("graph_explain", {"node": "blk_a"})

    assert envelope["ok"] is False
    assert "spec:blk_a" in envelope["candidates"]


def test_an_unknown_model_lists_the_models_that_exist(mcp_project: Path):
    """The model name is checked before spawning a viewer."""
    ts = _toolset(mcp_project)

    envelope = ts.call("find_module", {"model": "nope", "module": "fifo"})

    assert envelope["ok"] is False
    assert "unknown model" in envelope["error"]
    assert envelope["models"] == ["blk_a", "blk_b"]


def test_hub_tools_report_a_dead_hub_rather_than_crashing(mcp_project: Path):
    """The handle said yes at start; the socket may still refuse."""
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:1"))

    envelope = ts.call("hub_state", {})

    assert envelope["ok"] is False
    assert "hub" in envelope["error"].lower()


def test_hub_diagnose_validates_before_dialling(mcp_project: Path):
    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:1"))

    envelope = ts.call("hub_diagnose", {"source": "agent"})

    assert envelope["ok"] is False
    assert "clear" in envelope["error"]


def test_cli_list_tools_machine_envelope(mcp_project: Path):
    rb = RtlBuddy(name="test_mcp_server")
    result = CliRunner().invoke(rb.app, ["--machine", "mcp", "--list-tools"])
    rb._artifact_locks.release_all()

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])["payload"]
    assert [tool["name"] for tool in payload["tools"]] == list(STATELESS_TOOL_NAMES)
    assert payload["hub"]["present"] is False
    assert payload["sdk"]["available"] is mcp_server.sdk_available()
    # camelCase on the wire, as MCP spells it.
    assert "inputSchema" in payload["tools"][0]


def test_a_missing_sdk_is_a_configuration_error_with_an_install_hint(
    monkeypatch: pytest.MonkeyPatch,
):
    from rtl_buddy.errors import FatalRtlBuddyError

    monkeypatch.setattr(mcp_server, "sdk_available", lambda: False)

    with pytest.raises(FatalRtlBuddyError) as excinfo:
        mcp_server.require_sdk()

    assert "rtl_buddy[mcp]" in str(excinfo.value)
    assert "--machine" in str(excinfo.value)


@requires_sdk
def test_schemas_validate_against_the_sdk_tool_model(mcp_project: Path):
    import mcp.types as types

    ts = _toolset(mcp_project, hub=HubHandle(present=True, tcp="127.0.0.1:9999"))

    tools = [types.Tool.model_validate(mcp_server.tool_payload(s)) for s in ts.specs()]

    assert [tool.name for tool in tools] == ts.names()


@requires_sdk
def test_a_failed_call_is_flagged_is_error_for_hosts_that_only_read_the_flag(
    mcp_project: Path,
):
    import mcp.types as types

    ts = _toolset(mcp_project)
    payload = mcp_server.result_payload(ts.call("graph_query", {}))
    result = types.CallToolResult.model_validate(payload)

    assert result.is_error is True
    assert json.loads(result.content[0].text)["ok"] is False


@requires_sdk
def test_build_server_wires_the_toolset_to_an_sdk_server(mcp_project: Path):
    ts = _toolset(mcp_project)

    server = mcp_server.build_server(ts)

    assert server.server_info.name == mcp_server.SERVER_NAME


@pytest.mark.skipif(
    not _HAS_SDK or shutil.which("rb") is None,
    reason="needs the `mcp` SDK and an installed `rb` entry point",
)
def test_stdio_server_lists_and_calls_tools_over_the_wire(mcp_project: Path):
    """A fresh checkout with no hub answers tool calls."""
    proc = subprocess.Popen(
        [shutil.which("rb"), "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=str(mcp_project),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    def send(message: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def recv() -> dict:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line, "server closed the stream"
        return json.loads(line)

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            }
        )
        assert "result" in recv()
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = recv()
        assert [t["name"] for t in listed["result"]["tools"]] == list(
            STATELESS_TOOL_NAMES
        )

        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "graph_query",
                    "arguments": {"question": "which tests cover A-COV-1"},
                },
            }
        )
        called = recv()
        envelope = json.loads(called["result"]["content"][0]["text"])
        assert envelope["ok"] is True
        assert envelope["payload"]["matches"][0]["id"] == "covitem:blk_a#A-COV-1"
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - hung server
            proc.kill()
            proc.wait(timeout=10)
