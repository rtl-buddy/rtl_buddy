"""Contract tests for `rb cdc --emit-constraints` generation (#291).

Drives the pure generator (`tools/cdc_constraints.generate_constraints`)
against checked-in rtl-buddy-cdc map fixtures — the reference synchronizer set
(single-bit 2FF, multi-bit gray bus, req/ack handshake, reset sync). No live
tool: the maps are the contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_buddy.tools.cdc_constraints import generate_constraints

FIX = Path(__file__).parent / "fixtures" / "cdc"


@pytest.fixture
def domain_map():
    return json.loads((FIX / "cdc_ref_domain_map.json").read_text())


@pytest.fixture
def reset_map():
    return json.loads((FIX / "cdc_ref_reset_map.json").read_text())


def _kinds(result):
    from collections import Counter

    return Counter(e["kind"] for e in result.manifest)


def test_emit_xdc_top_has_clock_framing_and_per_crossing_exceptions(
    domain_map, reset_map
):
    r = generate_constraints(domain_map, reset_map, fmt="xdc", scoped=False)
    k = _kinds(r)
    # 5 async crossings -> 5 max_delay; the two width-8 buses -> 2 bus_skew
    assert k["max_delay"] == 5
    assert k["bus_skew"] == 2
    # two clocks echoed + one async group at the top
    assert k["create_clock"] == 2
    assert k["clock_groups"] == 1
    # four reset-sync flops collapse to two synchronizer instances
    assert k["reset_false_path"] == 2

    text = r.text
    assert "create_clock -name clk_a -period 8.0" in text
    assert "set_clock_groups -asynchronous -group {clk_a} -group {clk_b}" in text
    # max_delay is bounded to the *destination* period, not the launch period.
    # The whole-domain source (the bare top) is addressed by its launch clock
    # (canonical CDC -from, all valid startpoints); the destination is a rooted,
    # sequential-only instance selector — never the `-hierarchical <inst>/*`
    # form Vivado binds to nothing, and never the bare `<inst>/*` that drags in
    # combinational / VCC / clock-buffer cells (18-401/18-402 noise).
    assert (
        "set_max_delay -datapath_only 10.0 -from [get_clocks {clk_a}]"
        " -to [get_cells u_flag_sync/* -filter {IS_SEQUENTIAL}]" in text
    )
    # the clk_b->clk_a ack crossing is bounded to clk_a's 8.0 ns
    assert "set_max_delay -datapath_only 8.0" in text
    # reset synchronizers get their own false_path, not a data exception
    assert "set_false_path -to [get_cells u_rst_a/* -filter {IS_SEQUENTIAL}]" in text
    assert "set_false_path -to [get_cells u_rst_b/* -filter {IS_SEQUENTIAL}]" in text
    # path-exception start/endpoints are sequential-only
    assert "-from [get_cells u_hs/* -filter {IS_SEQUENTIAL}]" in text
    # the broken `-hierarchical <inst>/*` selector must never be emitted
    assert "-hierarchical u_" not in text
    assert "-hierarchical cdc_ref_top" not in text


def test_bus_skew_only_on_multibit_crossings(domain_map, reset_map):
    r = generate_constraints(domain_map, reset_map, fmt="xdc")
    bus_targets = {e["target"] for e in r.manifest if e["kind"] == "bus_skew"}
    # gray bus and handshake data bus are width 8; the 1-bit syncs are not skewed
    assert "u_gray_sync" in bus_targets
    assert all(e["width"] > 1 for e in r.manifest if e["kind"] == "bus_skew")
    assert all(
        e["width"] == 1
        for e in r.manifest
        if e["kind"] == "max_delay"
        and e["target"] in ("u_flag_sync", "u_hs/u_req_sync", "u_hs/u_ack_sync")
    )


def test_scoped_omits_top_clock_framing_and_uses_relative_cells(domain_map, reset_map):
    r = generate_constraints(domain_map, reset_map, fmt="xdc", scoped=True)
    k = _kinds(r)
    # scoped IP emit: no top-level clock defs / groups
    assert k["create_clock"] == 0
    assert k["clock_groups"] == 0
    # data + reset exceptions remain, with IP-relative (non-hierarchical) cells
    assert k["max_delay"] == 5
    assert k["reset_false_path"] == 2
    assert "SCOPED_TO_REF" in r.text
    # rooted/relative, sequential-only instance selector
    assert "[get_cells u_flag_sync/* -filter {IS_SEQUENTIAL}]" in r.text
    # the broken `-hierarchical <inst>/*` form must never appear (scoped uses a
    # filtered `-hierarchical *` for the whole-domain source, which is valid)
    assert "-hierarchical u_" not in r.text
    assert "-hierarchical cdc_ref_top" not in r.text


def test_sdc_and_xdc_share_the_cdc_subset(domain_map, reset_map):
    sdc = generate_constraints(domain_map, reset_map, fmt="sdc")
    xdc = generate_constraints(domain_map, reset_map, fmt="xdc")
    # the CDC-relevant commands are identical; only the header differs
    assert sdc.manifest == xdc.manifest
    body = lambda t: "\n".join(  # noqa: E731
        ln for ln in t.splitlines() if not ln.startswith("#")
    )
    assert body(sdc.text) == body(xdc.text)


def test_missing_period_is_flagged_not_guessed(reset_map):
    # A crossing whose dst clock has no period must not emit a bogus number.
    dm = {
        "design": {"top": "t"},
        "clocks": [{"name": "clk_a", "period": 8.0, "ports": ["clk_a"]}],
        "clock_groups": [],
        "crossings": [
            {
                "src_clock": "clk_a",
                "dst_clock": "clk_b",  # no period defined
                "src_source_instance_path": "t.a",
                "dst_source_instance_path": "t.b",
                "width": 1,
                "async_per_sdc": True,
            }
        ],
    }
    r = generate_constraints(dm, {}, fmt="sdc")
    assert not any(e["kind"] == "max_delay" for e in r.manifest)
    assert "WARNING: no period for clk_b" in r.text


def test_unknown_format_raises():
    with pytest.raises(ValueError, match="unknown constraint format"):
        generate_constraints({"crossings": []}, {}, fmt="qsf")


def test_emitted_xdc_round_trips_through_check_xdc_clean(domain_map, reset_map):
    """The generated XDC, fed back through the audit, is coverage-complete with
    zero over-waive — i.e. emit and check-xdc agree on the cell-selector syntax.

    This is the guard the binding bug needed: emit and audit must parse the same
    selector grammar, so a generated file always audits clean.
    """
    from rtl_buddy.tools.cdc_xdc_audit import audit_xdc, extract_cdc_constraints

    report = json.loads((FIX / "cdc_ref_report.json").read_text())
    emitted = generate_constraints(domain_map, reset_map, fmt="xdc")
    xc = extract_cdc_constraints(emitted.text)
    res = audit_xdc(domain_map, report, xc)
    assert res.blockers == [], [f.message for f in res.blockers]
    assert res.findings == [], [f.message for f in res.findings]


def test_emitted_scoped_xdc_round_trips_clean_coverage(domain_map, reset_map):
    """Same emit -> check-xdc round-trip for the ``scoped`` path.

    The scoped whole-domain source is a *different* selector
    (``[get_cells -hierarchical * -filter {IS_SEQUENTIAL}]``) that exercises a
    separate branch of the audit's ``_tokens`` parser, so it needs its own
    guard. A scoped IP file omits the top-level clock framing (clocks belong to
    the instantiating parent), so the audit legitimately reports two
    ``clock_graph`` warnings — but the *coverage* must still be complete: no
    ``unconstrained_crossing`` (every crossing's selector parsed and matched)
    and no ``over_waive``, and no blockers.
    """
    from rtl_buddy.tools.cdc_xdc_audit import audit_xdc, extract_cdc_constraints

    report = json.loads((FIX / "cdc_ref_report.json").read_text())
    emitted = generate_constraints(domain_map, reset_map, fmt="xdc", scoped=True)
    xc = extract_cdc_constraints(emitted.text)
    res = audit_xdc(domain_map, report, xc)
    assert res.blockers == [], [f.message for f in res.blockers]
    kinds = {f.kind for f in res.findings}
    assert "unconstrained_crossing" not in kinds, [f.message for f in res.findings]
    assert "over_waive" not in kinds, [f.message for f in res.findings]
    # the only findings are the expected clock_graph warnings (no create_clock
    # in a scoped IP file)
    assert kinds <= {"clock_graph"}, [f.message for f in res.findings]


# ---------------------------------------------------------------------------
# RtlBuddyCdc(emit_maps=True) — argv plumbing + map readback
# ---------------------------------------------------------------------------


def test_emit_maps_adds_flags_and_reads_back(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from rtl_buddy.config.cdc import CdcConfig, CdcToolConfig, CdcToolConfigFile
    from rtl_buddy.config.cdc import CdcToolOptsFile
    from rtl_buddy.config.model import ModelConfig
    from rtl_buddy.process_utils import ManagedProcessResult
    from rtl_buddy.tools import cdc_rtl_buddy as mod
    from rtl_buddy.tools.cdc_rtl_buddy import RtlBuddyCdc

    sv = tmp_path / "top.sv"
    sv.write_text("module my_module(); endmodule")
    sdc = tmp_path / "my_module.sdc"
    sdc.write_text("# empty SDC")
    model = ModelConfig(name="my_module", filelist=[f"-v {sv}"], path=str(tmp_path))
    cdc_cfg = CdcConfig(
        name="emit_cdc",
        desc="t",
        model=model,
        tool="rtl-buddy-cdc",
        constraints=str(sdc),
        waivers=None,
        _reglvl=None,
        tool_overrides=None,
        frontend=None,
    )
    tool_cfg = CdcToolConfig(
        CdcToolConfigFile(
            name="rtl-buddy-cdc", tool="rtl-buddy-cdc", opts=CdcToolOptsFile()
        )
    )
    wrapper = RtlBuddyCdc(
        name="t",
        cdc_cfg=cdc_cfg,
        tool_cfg=tool_cfg,
        suite_dir=str(tmp_path),
        emit_maps=True,
    )
    json_report = Path(wrapper.artefact_dir) / "cdc.json"
    calls: list[list[str]] = []

    def _fake_run(cmd, stdout, stderr, **kwargs):
        calls.append(list(cmd))
        json_report.write_text('{"summary": {"violations": 0, "suppressed": 0}}')
        # The real tool writes the maps at the requested paths; emulate that.
        Path(wrapper._domain_map_path()).write_text('{"crossings": [], "clocks": []}')
        Path(wrapper._reset_map_path()).write_text('{"reset_synchronizers": []}')
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(mod, "task_status", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(mod, "run_managed_process", _fake_run)
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)

    wrapper.run()
    # the analysis (json + text) invocations both carry the emit-map flags
    assert any(
        "--emit-domain-map" in c and "--emit-reset-domain-map" in c for c in calls
    )
    dm, rm = wrapper.read_emitted_maps()
    assert dm == {"crossings": [], "clocks": []}
    assert rm == {"reset_synchronizers": []}


def test_read_emitted_maps_missing_returns_none(tmp_path):
    from rtl_buddy.config.cdc import CdcConfig, CdcToolConfig, CdcToolConfigFile
    from rtl_buddy.config.cdc import CdcToolOptsFile
    from rtl_buddy.config.model import ModelConfig
    from rtl_buddy.tools.cdc_rtl_buddy import RtlBuddyCdc

    model = ModelConfig(name="m", filelist=[], path=str(tmp_path))
    cdc_cfg = CdcConfig(
        name="emit_cdc",
        desc="t",
        model=model,
        tool="rtl-buddy-cdc",
        constraints=str(tmp_path / "x.sdc"),
        waivers=None,
        _reglvl=None,
        tool_overrides=None,
        frontend=None,
    )
    tool_cfg = CdcToolConfig(
        CdcToolConfigFile(
            name="rtl-buddy-cdc", tool="rtl-buddy-cdc", opts=CdcToolOptsFile()
        )
    )
    wrapper = RtlBuddyCdc(
        name="t", cdc_cfg=cdc_cfg, tool_cfg=tool_cfg, suite_dir=str(tmp_path)
    )
    assert wrapper.read_emitted_maps() == (None, None)
