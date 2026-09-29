"""Contract tests for `rb cdc --check-xdc`.

Drives the pure XDC extractor and audit (`tools/cdc_xdc_audit`) against the shared reference synchronizer fixtures and a negative fixture with an unsynchronized crossing. No live tool runs; the checked-in rtl-buddy-cdc maps and report are the contract.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest


from rtl_buddy.tools.cdc_xdc_audit import audit_xdc, extract_cdc_constraints

FIX = Path(__file__).parent / "fixtures" / "cdc"


def _audit(domain_map, report, xdc):
    dm = json.loads((FIX / domain_map).read_text())
    rep = json.loads((FIX / report).read_text())
    xc = extract_cdc_constraints((FIX / xdc).read_text())
    return audit_xdc(dm, rep, xc)


def _kinds(res):
    from collections import Counter

    return Counter(f.kind for f in res.findings)


def test_extract_pulls_cdc_subset_and_ignores_io_placement(constraint_backend):
    xdc = """
    create_clock -name clk_a -period 8.0 [get_ports {clk_a}]
    create_clock -name clk_b -period 10.0 [get_ports {clk_b}]
    set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
    set_max_delay -datapath_only 10.0 -from [get_cells -hierarchical u_x/*] -to [get_cells -hierarchical u_y/*]
    set_bus_skew 10.0 -from [get_cells u_x/*] -to [get_cells u_y/*]
    set_false_path -from [get_clocks clk_b] -to [get_clocks clk_a]
    # not CDC — must be ignored:
    set_property IOSTANDARD LVCMOS18 [get_ports clk_a]
    set_property PACKAGE_PIN A1 [get_ports clk_a]
    create_pblock pblock_x
    """
    xc = extract_cdc_constraints(xdc)
    assert xc.clocks == {"clk_a": 8.0, "clk_b": 10.0}
    assert frozenset({"clk_a", "clk_b"}) in xc.async_clock_pairs
    kinds = sorted(e.kind for e in xc.path_exceptions)
    assert kinds == ["bus_skew", "false_path", "max_delay"]
    fp = next(e for e in xc.path_exceptions if e.kind == "false_path")
    assert "clk_b" in fp.from_clocks and "clk_a" in fp.to_clocks
    assert "A1" not in xc.clocks


def test_clock_groups_expands_all_cross_group_pairs(constraint_backend):
    xc = extract_cdc_constraints(
        "set_clock_groups -asynchronous -group {clk_a clk_a2} -group {clk_b}"
    )
    assert frozenset({"clk_a", "clk_b"}) in xc.async_clock_pairs
    assert frozenset({"clk_a2", "clk_b"}) in xc.async_clock_pairs
    assert frozenset({"clk_a", "clk_a2"}) not in xc.async_clock_pairs


def test_nested_collection_yields_the_instance_not_the_bracket_head(constraint_backend):
    # A Tcl-bracketed cell list is one token; a regex cutting at the first `]` would split it.
    xc = extract_cdc_constraints(
        "set_false_path -from [get_pins [get_cells u_a]/C] -to [get_cells u_b]\n"
    )
    [fp] = xc.path_exceptions
    assert fp.from_cells == ["u_a"]
    assert fp.to_cells == ["u_b"]


def test_hash_inside_braces_is_part_of_the_name(constraint_backend):
    # A `#` inside a clock definition is not a comment start.
    xc = extract_cdc_constraints(
        "create_clock -name clk#1 -period 10 [get_ports {clk#1}]\n"
    )
    assert xc.clocks == {"clk#1": 10.0}


def test_continued_false_path_keeps_both_endpoints(constraint_backend):
    # A continued -from/-to must not be read as one-sided, or the audit misses a masked crossing.
    xc = extract_cdc_constraints(
        "set_false_path -from [get_clocks clk_a] \\\n-to [get_clocks clk_b]\n"
    )
    [fp] = xc.path_exceptions
    assert fp.from_clocks == ["clk_a"]
    assert fp.to_clocks == ["clk_b"]
    assert fp.raw == "set_false_path -from [get_clocks clk_a] -to [get_clocks clk_b]"


def test_braced_period_is_read(constraint_backend):
    xc = extract_cdc_constraints(
        "create_clock -name clk -period {10.0} [get_ports clk] # main\n"
    )
    assert xc.clocks == {"clk": 10.0}


def test_clock_groups_mixes_brace_and_bracket_groups(constraint_backend):
    xc = extract_cdc_constraints(
        "set_clock_groups -asynchronous -group {a b} -group [get_clocks c]\n"
    )
    assert xc.async_clock_pairs == {frozenset({"a", "c"}), frozenset({"b", "c"})}


def test_continued_clock_groups_sees_every_group(constraint_backend):
    xc = extract_cdc_constraints(
        "set_clock_groups -asynchronous \\\n  -group {clk_a} \\\n  -group {clk_b}\n"
    )
    assert frozenset({"clk_a", "clk_b"}) in xc.async_clock_pairs


def test_commented_out_constraint_is_ignored(constraint_backend):
    xc = extract_cdc_constraints(
        "# set_false_path -from [get_clocks a] -to [get_clocks b]\n"
        "create_clock -name a -period 1 [get_ports a]\n"
    )
    assert xc.path_exceptions == []
    assert xc.clocks == {"a": 1.0}


def test_variable_period_warns_once_per_file(tokenizer_backend, caplog):
    xdc = (
        "set p 10\n"
        "create_clock -name a -period $p [get_ports a]\n"
        "create_clock -name b -period $p [get_ports b]\n"
    )
    with caplog.at_level(logging.WARNING):
        xc = extract_cdc_constraints(xdc, source="v.xdc")
    assert xc.clocks == {"a": None, "b": None}
    skipped = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "constraints.tokenizer_skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0].rtl_fields["source"] == "v.xdc"


def test_variable_period_is_evaluated_under_the_interp(tcl_backend, caplog):
    # With an interpreter the period is a number, so the audit compares real periods.
    xdc = (
        "set p 10\n"
        "create_clock -name a -period $p [get_ports a]\n"
        "create_clock -name b -period [expr {$p * 2}] [get_ports b]\n"
    )
    with caplog.at_level(logging.WARNING):
        xc = extract_cdc_constraints(xdc, source="v.xdc")
    assert xc.clocks == {"a": 10.0, "b": 20.0}
    assert not [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "constraints.tokenizer_skipped"
    ]


def test_plain_xdc_does_not_warn(constraint_backend, caplog):
    xdc = (
        "create_clock -name clk -period 10 [get_ports clk]\n"
        "set_property IOSTANDARD LVCMOS18 [get_ports clk]\n"
    )
    with caplog.at_level(logging.WARNING):
        extract_cdc_constraints(xdc, source="plain.xdc")
    assert not [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "constraints.tokenizer_skipped"
    ]


def test_continued_waiver_is_seen_as_two_sided_by_the_audit(constraint_backend):
    # A continued -from/-to read as one-sided would make a correct waiver look missing.
    dm = json.loads((FIX / "cdc_bad_domain_map.json").read_text())
    rep = json.loads((FIX / "cdc_bad_report.json").read_text())
    one_line = (
        "create_clock -name clk_a -period 8.0 [get_ports {clk_a}]\n"
        "create_clock -name clk_b -period 10.0 [get_ports {clk_b}]\n"
        "set_false_path -from [get_clocks clk_a] -to [get_clocks clk_b]\n"
    )
    continued = (
        "create_clock -name clk_a -period 8.0 [get_ports {clk_a}]\n"
        "create_clock -name clk_b -period 10.0 [get_ports {clk_b}]\n"
        "set_false_path -from [get_clocks clk_a] \\\n  -to [get_clocks clk_b]\n"
    )
    a = _kinds(audit_xdc(dm, rep, extract_cdc_constraints(one_line)))
    b = _kinds(audit_xdc(dm, rep, extract_cdc_constraints(continued)))
    assert a == b


def test_good_xdc_audits_clean():
    res = _audit("cdc_ref_domain_map.json", "cdc_ref_report.json", "cdc_ref_good.xdc")
    assert res.findings == [], [f.message for f in res.findings]
    assert res.blockers == []


def test_completeness_gap_flags_unconstrained_crossing():
    res = _audit("cdc_ref_domain_map.json", "cdc_ref_report.json", "cdc_ref_gappy.xdc")
    assert _kinds(res)["unconstrained_crossing"] == 1
    f = next(f for f in res.findings if f.kind == "unconstrained_crossing")
    assert f.severity == "blocker"
    assert f.target == "u_flag_sync"


def test_false_path_on_bus_flags_missing_bus_skew():
    res = _audit("cdc_ref_domain_map.json", "cdc_ref_report.json", "cdc_ref_busfp.xdc")
    assert _kinds(res)["missing_bus_skew"] == 2
    assert all(
        f.severity == "warning" for f in res.findings if f.kind == "missing_bus_skew"
    )
    assert _kinds(res)["unconstrained_crossing"] == 0
    assert _kinds(res)["over_waive"] == 0


def test_over_waive_flags_unsynchronized_crossing_as_blocker():
    res = _audit(
        "cdc_bad_domain_map.json", "cdc_bad_report.json", "cdc_bad_overwaive.xdc"
    )
    assert _kinds(res)["over_waive"] == 1
    f = next(f for f in res.findings if f.kind == "over_waive")
    assert f.severity == "blocker"
    assert (f.src_clock, f.dst_clock) == ("clk_a", "clk_b")
    assert "masks a real metastability bug" in f.message


def test_max_delay_on_unsync_crossing_is_not_over_waive():
    # max_delay still times the path, so it is not a dangerous waive on an unsynchronized crossing.
    xdc = (
        "create_clock -name clk_a -period 8.0 [get_ports {clk_a}]\n"
        "create_clock -name clk_b -period 10.0 [get_ports {clk_b}]\n"
        "set_max_delay -datapath_only 10.0 -from [get_clocks clk_a] -to [get_clocks clk_b]\n"
    )
    dm = json.loads((FIX / "cdc_bad_domain_map.json").read_text())
    rep = json.loads((FIX / "cdc_bad_report.json").read_text())
    res = audit_xdc(dm, rep, extract_cdc_constraints(xdc))
    assert _kinds(res)["over_waive"] == 0


def test_bare_max_delay_does_not_count_as_coverage():
    # A `set_max_delay` without -datapath_only times the clock relationship, so it does not cover the crossing.
    dm = {
        "design": {"top": "t"},
        "clocks": [
            {"name": "clk_a", "period": 8.0},
            {"name": "clk_b", "period": 10.0},
        ],
        "clock_groups": [],
        "crossings": [
            {
                "src_clock": "clk_a",
                "dst_clock": "clk_b",
                "src_source_instance_path": "t.a",
                "dst_source_instance_path": "t.u_sync",
                "width": 1,
                "async_per_sdc": True,
            }
        ],
    }
    clocks = (
        "create_clock -name clk_a -period 8 [get_ports clk_a]\n"
        "create_clock -name clk_b -period 10 [get_ports clk_b]\n"
    )
    bare = (
        clocks + "set_max_delay 10.0 -from [get_clocks clk_a] -to [get_clocks clk_b]\n"
    )
    res = audit_xdc(dm, {}, extract_cdc_constraints(bare))
    assert _kinds(res)["unconstrained_crossing"] == 1

    dp = clocks + (
        "set_max_delay -datapath_only 10.0 -from [get_clocks clk_a] "
        "-to [get_clocks clk_b]\n"
    )
    res2 = audit_xdc(dm, {}, extract_cdc_constraints(dp))
    assert _kinds(res2)["unconstrained_crossing"] == 0


def test_clock_graph_flags_missing_and_extra_clocks():
    xdc = (
        "create_clock -name clk_a -period 8.0 [get_ports {clk_a}]\n"
        "create_clock -name clk_ghost -period 5.0 [get_ports {clk_ghost}]\n"
        "set_clock_groups -asynchronous -group {clk_a} -group {clk_b}\n"
    )
    dm = json.loads((FIX / "cdc_ref_domain_map.json").read_text())
    rep = json.loads((FIX / "cdc_ref_report.json").read_text())
    res = audit_xdc(dm, rep, extract_cdc_constraints(xdc))
    msgs = [f.message for f in res.findings if f.kind == "clock_graph"]
    assert any("clk_ghost" in m for m in msgs)
    assert any("clk_b" in m and "no create_clock" in m for m in msgs)


def test_recognized_sync_suppresses_false_over_waive():
    # A recognized synchronizer suppresses the over-waive finding for a correct waiver, but the crossing must still be covered.
    dm = json.loads((FIX / "cdc_xpm_domain_map.json").read_text())
    rep = json.loads((FIX / "cdc_xpm_report.json").read_text())
    xc = extract_cdc_constraints((FIX / "cdc_xpm_overwaive.xdc").read_text())

    base = audit_xdc(dm, rep, xc)
    assert _kinds(base)["over_waive"] == 1

    recog = audit_xdc(dm, rep, xc, recognized_syncs=["u_xpm_single"])
    assert _kinds(recog)["over_waive"] == 0
    assert _kinds(recog)["unconstrained_crossing"] == 0


def test_recognized_sync_still_requires_coverage():
    # Recognition suppresses over-waive, not the coverage requirement.
    dm = json.loads((FIX / "cdc_xpm_domain_map.json").read_text())
    rep = json.loads((FIX / "cdc_xpm_report.json").read_text())
    bare_clocks = (
        "create_clock -name clk_a -period 8 [get_ports clk_a]\n"
        "create_clock -name clk_b -period 10 [get_ports clk_b]\n"
    )
    res = audit_xdc(
        dm, rep, extract_cdc_constraints(bare_clocks), recognized_syncs=["u_xpm_single"]
    )
    assert _kinds(res)["unconstrained_crossing"] == 1


def test_recognized_syncs_parses_from_cdc_yaml():
    from serde.yaml import from_yaml

    from rtl_buddy.config.cdc import CdcSuiteConfigFile

    y = (
        "rtl-buddy-filetype: cdc_config\n"
        "analyses:\n"
        "  - name: a\n"
        "    desc: d\n"
        "    model: m\n"
        "    model_path: models.yaml\n"
        "    tool: rtl-buddy-cdc\n"
        "    constraints: a.sdc\n"
        '    recognized-syncs: ["u_xpm.*", "xpm_cdc_single"]\n'
    )
    cfg = from_yaml(CdcSuiteConfigFile, y)
    assert cfg.analyses[0].recognized_syncs == ["u_xpm.*", "xpm_cdc_single"]
    y2 = y.replace('    recognized-syncs: ["u_xpm.*", "xpm_cdc_single"]\n', "")
    assert from_yaml(CdcSuiteConfigFile, y2).analyses[0].recognized_syncs == []


def test_invalid_recognized_sync_regex_is_config_error():
    from rtl_buddy.errors import FatalRtlBuddyError

    dm = json.loads((FIX / "cdc_xpm_domain_map.json").read_text())
    rep = json.loads((FIX / "cdc_xpm_report.json").read_text())
    with pytest.raises(FatalRtlBuddyError, match="invalid recognized-syncs regex"):
        audit_xdc(dm, rep, extract_cdc_constraints(""), recognized_syncs=["u_xpm_["])


def test_machine_payload_shape():
    res = _audit(
        "cdc_bad_domain_map.json", "cdc_bad_report.json", "cdc_bad_overwaive.xdc"
    )
    rows = res.to_machine()
    assert rows and all(
        {"severity", "kind", "message", "src_clock", "dst_clock", "target"} <= r.keys()
        for r in rows
    )


# A flattening frontend collapses every capture instance to the top, so cell-scoped exceptions cannot be matched.
_FLATTENED_MAP = {
    "design": {"top": "ip_top"},
    "clocks": [{"name": "clk_a", "period": 8.0}, {"name": "clk_b", "period": 10.0}],
    "crossings": [
        {
            "src_clock": "clk_a",
            "dst_clock": "clk_b",
            "dst_source_instance_path": "ip_top",
            "width": 1,
            "async_per_sdc": True,
        }
    ],
}


def test_flattened_map_warns_when_xdc_uses_cell_scope():
    # Cell-scoped exceptions on a flattened map warn.
    xc = extract_cdc_constraints(
        "set_max_delay -datapath_only 10.0 -from [get_cells u_x/*] "
        "-to [get_cells u_sync/*]\n"
    )
    res = audit_xdc(_FLATTENED_MAP, {"violations": []}, xc)
    assert _kinds(res)["frontend_flattened"] == 1
    f = next(f for f in res.findings if f.kind == "frontend_flattened")
    assert f.severity == "warning" and "frontend: slang" in f.message


def test_flattened_map_no_warning_for_clock_only_xdc():
    # A clock-group waiver audits without a warning on a flattened map.
    xc = extract_cdc_constraints(
        "set_clock_groups -asynchronous -group {clk_a} -group {clk_b}\n"
    )
    res = audit_xdc(_FLATTENED_MAP, {"violations": []}, xc)
    assert _kinds(res)["frontend_flattened"] == 0
