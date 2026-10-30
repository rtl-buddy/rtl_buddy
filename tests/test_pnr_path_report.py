"""Tests for the worst-path and fanout reports of `rb pnr`'s final reports (rtl_buddy#944): `worst_paths.rpt`, `fanout.rpt` and their result fields."""

import shutil
import subprocess
from textwrap import dedent

import pytest

from rtl_buddy.config.pnr import PnrSuiteConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_path_report as report

from test_multi_corner import _pdk, _platform as _mc_platform, _pnr_backend_over
from test_pnr import _make_pdk_cfg, _platform, _render_flow

# A hierarchical Yosys netlist: `u_core` holds a register with a named output, an auto-named cell with a `src`, and a macro.
_NETLIST = dedent("""\
    module core(clk, d, q);
      input clk;
      wire clk;
      input d;
      wire d;
      output [1:0] q;
      wire [1:0] q;
      wire _05_;
      (* src = "/proj/rtl/core.sv:12.3-14.6" *)
      DFF_X1 _10_ (
        .CK(clk),
        .D(_05_),
        .Q(q[1])
      );
      (* src = "/proj/rtl/core.sv:20.5-20.30|/proj/rtl/pkg.sv:3.1-3.9" *)
      NAND2_X1 _11_ (
        .A1(d),
        .A2(clk),
        .ZN(_05_)
      );
      sram_32x8 u_sram (
        .clk(clk),
        .din(d),
        .dout(q[0])
      );
      (* src = "/proj/rtl/core.sv:30.3-33.6" *)
      (* keep = 1 *)
      PARAM_CELL #(
        .W(8),
        .MODE("a;b")
      ) _12_ (
        .A(d),
        .Z(q[0])
      );
      (* src = "/proj/rtl/core.sv:40.3-41.6" *)
      DFF_X1 \\u_sub.state_reg[2] (
        .CK(clk),
        .D(d),
        .Q(_05_)
      );
    endmodule
    module demo_top(clk, d, q);
      input clk;
      wire clk;
      input d;
      wire d;
      output [1:0] q;
      wire [1:0] q;
      core u_core (
        .clk(clk),
        .d(d),
        .q(q)
      );
    endmodule
""")


def _write_netlist(tmp_path):
    path = tmp_path / "artefacts" / "demo_synth" / "synth_netlist.v"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_NETLIST)
    return str(path)


def _path(group, slack, levels, start, snets, end, enets, corner="-", buffers=0):
    return (
        f"{report.WORST_PATH_TAG}\t{group}\t{corner}\t{slack}\t{levels}\t{buffers}"
        f"\t{start}\t{snets}\t{end}\t{enets}\n"
    )


def _fanout(fanout, *hops):
    """`hops` are (net, driver pin, driver kind) triples, flattened."""
    return f"{report.FANOUT_TAG}\t{fanout}\t" + "\t".join(hops) + "\n"


def _run(tmp_path, extra=""):
    path = tmp_path / "pnr.yaml"
    path.write_text(
        dedent("""\
        rtl-buddy-filetype: pnr_config
        runs:
          - name: blk
            desc: block
            synth: s
            synth-path: synth.yaml
            floorplan: {utilization: 0.55}
            platform: p
        """)
        + extra
    )
    return PnrSuiteConfig(str(path)).get_runs("blk")[0]


def test_report_fanout_nets_defaults_to_ten_and_parses(tmp_path):
    assert _run(tmp_path).get_report_fanout_nets() == 10
    assert _run(tmp_path, "    report-fanout-nets: 3\n").get_report_fanout_nets() == 3


@pytest.mark.parametrize("value", ["0", "-1", "true", "2.5", "many"])
def test_report_fanout_nets_must_be_a_positive_integer(tmp_path, value):
    with pytest.raises(FatalRtlBuddyError, match="report-fanout-nets"):
        _run(tmp_path, f"    report-fanout-nets: {value}\n")


def test_the_final_reports_print_the_paths_and_nets_before_the_after_reports_hooks(
    tmp_path,
):
    text = _render_flow(tmp_path, _platform(_make_pdk_cfg(tmp_path)))
    block = text.index("foreach rb_group [sta::path_group_names]")
    assert text.index("> $OUT_DIR/electrical.rpt") < block
    assert block < text.index("rb_fanout_nets 10\n") < text.index(">>> Write outputs")
    assert 'set rb_path_corner "-"\n' in text
    assert "-corner $rb_path_corner" not in text


def test_an_unset_report_fanout_nets_renders_as_ten(tmp_path):
    platform = _platform(_make_pdk_cfg(tmp_path))
    unset = _render_flow(tmp_path, platform)
    assert _render_flow(tmp_path, platform, report_fanout_nets=10) == unset
    three = _render_flow(tmp_path, platform, report_fanout_nets=3)
    assert three == unset.replace("rb_fanout_nets 10\n", "rb_fanout_nets 3\n")


def test_a_multi_corner_run_finds_the_paths_at_the_setup_critical_corner(tmp_path):
    platform = _mc_platform(_pdk(tmp_path), sta_corners=["tt", "ss", "ff"])
    text = _render_flow(tmp_path, platform)
    assert text.index("proc rb_find_corner") < text.index("foreach rb_name {tt ss ff}")
    assert "set rb_corner_args [list -corner $rb_path_corner]" in text


def test_a_failing_report_does_not_stop_the_flow(tmp_path):
    """Both reports are report-only; an OpenROAD that cannot run them loses them, not the routed database."""
    tclsh = shutil.which("tclsh")
    if tclsh is None:
        pytest.skip("no tclsh")
    script = report.report_tcl(["tt", "ss"], 10) + 'puts "REACHED_WRITE_DB"\n'
    out = subprocess.run([tclsh], input=script, capture_output=True, text=True)
    assert "REACHED_WRITE_DB" in out.stdout
    assert "rb: worst path report unavailable:" in out.stdout
    assert "rb: fanout report unavailable:" in out.stdout


def test_names_resolve_to_rtl_names_where_the_netlist_keeps_them(tmp_path):
    log = (
        _path("clk", "-1.25e-10", 2, "u_core/_10_/Q", "u_core/q[1]", "q[0]", "")
        + _path("io", "3.0e-9", 0, "d", "", "u_core/u_sram/din", "")
        + _path(
            "asynchronous",
            "5e-9",
            1,
            "u_core/_11_/ZN",
            "u_core/_05_",
            "u_core/_10_/D",
            "net7",
            buffers=1,
        )
        + _fanout(12, "u_core/q[1]", "u_core/_10_/Q", "cell")
        + _fanout(
            9,
            "net42",
            "u_core/fanout3/X",
            "buf",
            "u_core/_05_",
            "u_core/_11_/ZN",
            "cell",
        )
        + _fanout(4, "net43", "u_core/split9/X", "buf", "net44", "rebuffer2/X", "cell")
        + _fanout(
            3, "net50", "u_core/inv3/Y", "inv", "u_core/q[1]", "u_core/_10_/Q", "cell"
        )
        + _fanout(2, "net51", "-", "-")
    )
    fields = report.write_reports(
        str(tmp_path), report.parse(log), _write_netlist(tmp_path), "demo_top", 10
    )

    assert fields == {
        "worst_path_levels": 2,
        "worst_path_startpoint": "u_core.q[1]",
        "worst_path_endpoint": "q[0]",
        "worst_path_resolved": True,
        "max_net_fanout": 12,
    }
    paths = (tmp_path / report.WORST_PATHS_REPORT).read_text()
    assert (
        "Path group: clk\n  Slack (ps):   -125.0\n  Logic levels: 2 (0 buffer(s)"
        in paths
    )
    assert (
        "Startpoint:   u_core.q[1] [u_core/_10_/Q; src /proj/rtl/core.sv:12]" in paths
    )
    assert "Endpoint:     u_core.u_sram.din [u_core/u_sram/din]" in paths
    # An auto-named cell with no named output is placed by its scope and source lines.
    assert (
        "Startpoint:   u_core (core.sv:20, pkg.sv:3) [u_core/_11_/ZN; "
        "src /proj/rtl/core.sv:20, /proj/rtl/pkg.sv:3]"
    ) in paths
    nets = (tmp_path / report.FANOUT_REPORT).read_text().splitlines()
    assert nets[3].split() == [
        "12",
        "u_core/q[1]",
        "<-",
        "u_core.q[1]",
        "[u_core/_10_/Q;",
        "src",
        "/proj/rtl/core.sv:12]",
    ]
    assert "net42  <-  u_core (core.sv:20, pkg.sv:3)" in nets[4]
    assert nets[4].endswith("(behind 1 buffer(s) or inverter(s))")
    # Cells P&R added are not in the netlist: the raw driver stays, marked.
    assert nets[5].endswith("net43  <-  u_core/split9/X (unresolved)")
    # An odd number of inverters between the net and its named driver inverts it.
    assert nets[6].endswith(
        "net50  <-  u_core.q[1] [u_core/_10_/Q; src /proj/rtl/core.sv:12] "
        "(inverted, behind 1 buffer(s) or inverter(s))"
    )
    assert nets[7].endswith("net51  <-  - (unresolved)")


def test_a_macro_is_named_by_the_pin_on_the_path(tmp_path):
    """A macro's own instance name wins over a net, at either end of a path."""
    log = _path(
        "m",
        "1e-10",
        1,
        "u_core/u_sram/dout",
        "u_core/q[0]",
        "u_core/u_sram/din",
        "u_core/q[0]",
    )
    fields = report.write_reports(
        str(tmp_path), report.parse(log), _write_netlist(tmp_path), "demo_top", 10
    )
    assert fields["worst_path_startpoint"] == "u_core.u_sram.dout"
    assert fields["worst_path_endpoint"] == "u_core.u_sram.din"


def test_the_reader_takes_multi_line_overrides_and_escaped_names(tmp_path):
    log = _path(
        "p",
        "1e-10",
        1,
        "u_core/_12_/Z",
        "u_core/_06_",
        "u_core/u_sub.state_reg\\[2\\]/D",
        "",
    )
    fields = report.write_reports(
        str(tmp_path), report.parse(log), _write_netlist(tmp_path), "demo_top", 10
    )
    assert fields["worst_path_startpoint"] == "u_core (core.sv:30)"
    assert fields["worst_path_endpoint"] == "u_core.u_sub.state_reg[2].D"


def test_a_name_the_netlist_lacks_stays_raw_and_unresolved(tmp_path):
    log = _path("clk", "1e-10", 3, "u_core/_10_/Q", "net9", "u_core/_99_/D", "_77_")
    fields = report.write_reports(
        str(tmp_path), report.parse(log), _write_netlist(tmp_path), "demo_top", 10
    )
    # The start cell's output net is P&R's, so its source line places it; the end cell is not in the netlist.
    assert fields["worst_path_startpoint"] == "u_core (core.sv:12)"
    assert fields["worst_path_endpoint"] == "u_core/_99_/D"
    assert fields["worst_path_resolved"] is False
    assert (
        "Endpoint:     u_core/_99_/D (unresolved)"
        in (tmp_path / report.WORST_PATHS_REPORT).read_text()
    )


def test_without_the_netlist_only_ports_resolve(tmp_path):
    log = _path("clk", "1e-10", 3, "clk_in", "", "u_core/_10_/D", "u_core/q[1]")
    fields = report.write_reports(
        str(tmp_path), report.parse(log), str(tmp_path / "missing.v"), "demo_top", 10
    )
    assert fields["worst_path_startpoint"] == "clk_in"
    assert fields["worst_path_endpoint"] == "u_core/_10_/D"
    assert fields["worst_path_resolved"] is False


def test_the_worst_group_gives_the_fields_and_escapes_are_dropped(tmp_path):
    log = _path("a", "2e-10", 5, "x", "", "y", "") + _path(
        "b", "-3e-10", "-", "u_core/_10_/Q", "u_core/q\\[1\\]", "z", ""
    )
    fields = report.write_reports(
        str(tmp_path), report.parse(log), _write_netlist(tmp_path), "demo_top", 10
    )
    # Group b is worse; its level count was not computed, so it is left out, never 0.
    assert "worst_path_levels" not in fields
    assert fields["worst_path_startpoint"] == "u_core.q[1]"
    assert (
        "Logic levels: unavailable"
        in (tmp_path / report.WORST_PATHS_REPORT).read_text()
    )


def test_a_report_that_could_not_run_is_unavailable(tmp_path):
    log = (
        'rb: worst path report unavailable: invalid command name "find_timing_paths"\n'
        "rb: fanout report unavailable: no such block\n"
    )
    fields = report.write_reports(str(tmp_path), report.parse(log), None, None, 10)
    assert fields == {}
    assert (
        'unavailable: invalid command name "find_timing_paths"'
        in (tmp_path / report.WORST_PATHS_REPORT).read_text()
    )
    assert "unavailable: no such block" in (tmp_path / report.FANOUT_REPORT).read_text()


def test_a_log_without_the_reports_says_so(tmp_path):
    fields = report.write_reports(str(tmp_path), report.parse(""), None, None, 10)
    assert fields == {}
    assert (
        "unavailable: no setup path was reported"
        in (tmp_path / report.WORST_PATHS_REPORT).read_text()
    )
    assert (
        "unavailable: no signal net was reported"
        in (tmp_path / report.FANOUT_REPORT).read_text()
    )


_FINAL = (
    ">>> Final reports\n"
    "worst slack max -0.12\n"
    "corner tt worst slack max 0.40\n"
    "corner ss worst slack max -0.12\n"
    "corner ff worst slack max 0.80\n"
)
_PATHS = _path(
    "clk", "-1.2e-10", 7, "u_core/_10_/Q", "u_core/q[1]", "q[0]", "", corner="ss"
) + _fanout(33, "u_core/q[1]", "u_core/_10_/Q", "cell")
_HOOK = (
    ">>> End of final reports\n"
    + _fanout(999, "hook_net", "hook/X", "cell")
    + _path("hook", "-9e-9", 1, "a", "", "b", "")
)


@pytest.mark.parametrize("corners", [None, ["tt", "ss", "ff"]], ids=["single", "multi"])
def test_a_routed_run_carries_the_fields_and_writes_both_reports(
    tmp_path, monkeypatch, corners
):
    """`rb pnr` reads the final reports only: what an after-reports hook prints is not the result."""
    platform = (
        _mc_platform(_pdk(tmp_path), sta_corners=corners)
        if corners
        else _mc_platform(_pdk(tmp_path))
    )
    _write_netlist(tmp_path)
    backend = _pnr_backend_over(
        tmp_path, monkeypatch, platform, _FINAL + _PATHS + _HOOK
    )
    res = backend.run().results

    assert res["result"] == "PASS"
    assert res["worst_path_levels"] == 7
    assert res["worst_path_startpoint"] == "u_core.q[1]"
    assert res["worst_path_endpoint"] == "q[0]"
    assert res["worst_path_resolved"] is True
    assert res["max_net_fanout"] == 33
    paths = (
        tmp_path / "artefacts" / "demo_pnr" / report.WORST_PATHS_REPORT
    ).read_text()
    assert paths.startswith(
        "Worst setup path of each path group, at the setup-critical corner ss\n"
    )
    assert "hook" not in paths
    assert (
        "hook_net"
        not in (tmp_path / "artefacts" / "demo_pnr" / report.FANOUT_REPORT).read_text()
    )


def test_a_routed_run_without_the_reports_leaves_the_fields_out(tmp_path, monkeypatch):
    platform = _mc_platform(_pdk(tmp_path))
    res = _pnr_backend_over(tmp_path, monkeypatch, platform, _FINAL).run().results
    for key in (
        "worst_path_levels",
        "worst_path_startpoint",
        "worst_path_endpoint",
        "worst_path_resolved",
        "max_net_fanout",
    ):
        assert key not in res


def test_a_report_that_cannot_be_written_does_not_change_the_verdict(
    tmp_path, monkeypatch, caplog
):
    def _fail(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(report, "write_reports", _fail)
    platform = _mc_platform(_pdk(tmp_path))
    with caplog.at_level("WARNING"):
        res = (
            _pnr_backend_over(tmp_path, monkeypatch, platform, _FINAL + _PATHS)
            .run()
            .results
        )
    assert res["result"] == "PASS"
    assert "worst_path_startpoint" not in res
    assert any(
        "worst_paths.rpt and fanout.rpt could not be written (OSError: disk full)"
        in r.getMessage()
        for r in caplog.records
    )
