"""An SDC mode per corner on a multi-corner `rb pnr` platform.

Each corner is timed in its own OpenSTA mode and scene, from one SDC or a per-corner `constraints:` mapping, with its own copy of the routed SPEF. Covered here: the config, the Tcl rendered with and without modes, and, under the stub OpenROAD of `test_pnr_checkpoints`, that a staged run's checkpoints carry every mode's constraints to the next session.
"""

import json
import os
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest
from test_pnr_checkpoints import _HARNESS, _REAL_RUN, _backend, _runs, _tcl_command

from rtl_buddy.config.pdk import PdkConfig, PdkConfigFile
from rtl_buddy.config.pnr import PnrConfig, PnrFloorplan, PnrSuiteConfig
from rtl_buddy.config.pnr_platform import (
    PnrPlatformConfig,
    PnrPlatformConfigFile,
    PnrRoutingLayersFile,
)
from rtl_buddy.errors import FatalRtlBuddyError

_CORNERS = ["tt", "ss", "ff"]


def _pdk(tmp_path, **changes):
    fields = {
        "name": "sky130hd",
        "site": "unithd",
        "corners": {c: f"pdk/lib/{c}.lib" for c in _CORNERS},
        "tech_lef": "pdk/lef/tech.lef",
        "macro_lef": "pdk/lef/cells.lef",
        "fill_cells": ["fill_1"],
    }
    for corner in _CORNERS:
        lib = tmp_path / "pdk" / "lib" / f"{corner}.lib"
        lib.parent.mkdir(parents=True, exist_ok=True)
        lib.write_text(f'library ({corner}) {{ time_unit : "1ns" ; }}\n')
    return PdkConfig(
        PdkConfigFile(**{**fields, **changes}), str(tmp_path / "root_config.yaml")
    )


def _platform(pdk, corners=_CORNERS, **changes):
    fields = dict(
        name="mc",
        pdk="sky130hd",
        cts_buffer="clkbuf_4",
        routing_layers=PnrRoutingLayersFile(signal="met1-met5", clock="met3-met5"),
    )
    if len(corners) > 1:
        fields["sta_corners"] = list(corners)
    else:
        fields["sta_corner"] = corners[0]
    return PnrPlatformConfig(
        PnrPlatformConfigFile(**{**fields, **changes}), lambda _name: pdk
    )


def _sdc(tmp_path, name, text="create_clock -name clk -period 10 [get_ports clk]\n"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def _cfg(tmp_path, constraints=None, **changes):
    fields = dict(
        name="demo_pnr",
        desc="demo",
        tool="openroad",
        synth_name="demo_synth",
        synth_suite_path=str(tmp_path / "synth.yaml"),
        constraints=constraints or _sdc(tmp_path, "constraints.sdc"),
        platform="mc",
        floorplan=PnrFloorplan(utilization=0.55, aspect=1.0, core_margin=2.0),
        _reglvl=1000,
        tool_overrides=None,
    )
    return PnrConfig(**{**fields, **changes})


def _render(tmp_path, platform, *, modes=True, checkpoints=False, **changes) -> str:
    """The `pnr.tcl` a one-session run renders: with an SDC mode per corner when `modes`, and with checkpoints armed when `checkpoints`."""
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    cfg = _cfg(tmp_path, **changes)
    top = MagicMock()
    top.get_top.return_value = "demo_top"
    top.get_name.return_value = "demo_synth"
    cfg.resolve_synth_cfg = MagicMock(return_value=top)
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=cfg,
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
    )
    backend._sta_modes = modes
    if checkpoints:
        backend._ckpt_run_dir = str(tmp_path / "ck" / "run")
    return Path(backend._write_script(platform, cfg.get_floorplan())).read_text()


def _lib(tmp_path, corner):
    return str(tmp_path / "pdk" / "lib" / f"{corner}.lib")


# ── config ─────────────────────────────────────────────────────────────────


def _load(tmp_path, constraints: str) -> PnrConfig:
    path = tmp_path / "pnr.yaml"
    path.write_text(
        dedent(f"""\
        rtl-buddy-filetype: pnr_config
        runs:
          - name: r
            desc: d
            synth: s
            synth-path: synth.yaml
            platform: p
            constraints: {constraints}
        """)
    )
    return PnrSuiteConfig(str(path)).get_runs("r")[0]


def test_constraints_takes_a_mapping_of_corner_to_sdc_resolved_like_a_path(tmp_path):
    cfg = _load(tmp_path, "{ss: sdc/slow.sdc, ff: ../fast.sdc}")

    assert cfg.get_constraints() == {
        "ss": str(tmp_path / "sdc" / "slow.sdc"),
        "ff": os.path.normpath(str(tmp_path / ".." / "fast.sdc")),
    }
    assert cfg.constraints_by_corner(["ff", "ss"]) == {
        "ff": os.path.normpath(str(tmp_path / ".." / "fast.sdc")),
        "ss": str(tmp_path / "sdc" / "slow.sdc"),
    }


def test_constraints_as_one_path_is_unchanged_and_serves_every_corner(tmp_path):
    cfg = _load(tmp_path, "sdc/top.sdc")

    path = str(tmp_path / "sdc" / "top.sdc")
    assert cfg.get_constraints() == path
    assert cfg.constraints_problem(_CORNERS) is None
    assert cfg.constraints_by_corner(_CORNERS) == {c: path for c in _CORNERS}


@pytest.mark.parametrize(
    "value, match",
    [
        ("{}", "must name each corner"),
        ("{ss: ''}", "corner 'ss' must map to an SDC path"),
        ("{ss: [a.sdc]}", "corner 'ss' must map to an SDC path"),
    ],
)
def test_constraints_mapping_rejects_an_entry_without_a_path(tmp_path, value, match):
    with pytest.raises(FatalRtlBuddyError, match=match):
        _load(tmp_path, value)


def test_constraints_mapping_names_a_missing_and_an_unknown_corner(tmp_path):
    cfg = _cfg(tmp_path, constraints={"tt": "a.sdc", "sss": "b.sdc"})

    problem = cfg.constraints_problem(_CORNERS)

    assert "no SDC for corner(s) ss, ff" in problem
    assert "corner(s) sss not among the platform's (tt, ss, ff)" in problem
    assert (
        _cfg(
            tmp_path, constraints=dict.fromkeys(_CORNERS, "a.sdc")
        ).constraints_problem(_CORNERS)
        is None
    )


# ── setup ──────────────────────────────────────────────────────────────────


def _setup_backend(tmp_path, platform, constraints, *, modes=True):
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_cfg(tmp_path, constraints=constraints),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
    )
    probes = []

    def _probe():
        probes.append(True)
        return modes

    backend._probe_sta_modes = _probe
    return backend, probes


def test_a_mapping_that_misses_a_corner_fails_setup_naming_it(tmp_path, caplog):
    platform = _platform(_pdk(tmp_path))
    backend, probes = _setup_backend(
        tmp_path, platform, {"tt": "a.sdc", "ss": "b.sdc", "fast": "c.sdc"}
    )

    failed = backend._check_corner_constraints(platform, "26Q4")

    assert failed.results["fail_stage"] == "setup"
    assert "no SDC for corner(s) ff" in failed.results["desc"]
    assert "corner(s) fast not among" in failed.results["desc"]
    assert not probes
    assert 'P&R "demo_pnr": constraints: no SDC for corner(s) ff' in caplog.text


def test_a_mapping_on_a_single_corner_platform_is_its_one_path(tmp_path):
    platform = _platform(_pdk(tmp_path), corners=["ss"])
    backend, probes = _setup_backend(tmp_path, platform, {"ss": "/x/slow.sdc"})

    assert backend._check_corner_constraints(platform, "26Q4") is None

    assert backend.pnr_cfg.get_constraints() == "/x/slow.sdc"
    assert not probes


def test_a_single_corner_mapping_renders_as_its_one_path(tmp_path):
    platform = _platform(_pdk(tmp_path), corners=["ss"])
    sdc = _sdc(tmp_path, "slow.sdc")
    plain = _render(tmp_path, platform, constraints=sdc)
    backend, _ = _setup_backend(tmp_path, platform, {"ss": sdc})
    backend._check_corner_constraints(platform, None)

    assert (
        _render(tmp_path, platform, constraints=backend.pnr_cfg.get_constraints())
        == plain
    )


def test_without_modes_every_corner_shares_one_mode_with_a_warning(tmp_path, caplog):
    platform = _platform(_pdk(tmp_path))
    backend, probes = _setup_backend(
        tmp_path, platform, _sdc(tmp_path, "top.sdc"), modes=False
    )

    assert backend._check_corner_constraints(platform, "26Q2-911") is None

    assert probes
    assert backend._sta_modes is False
    assert "cannot switch between SDC modes" in caplog.text
    assert "26Q2-911" in caplog.text


def test_without_modes_a_per_corner_sdc_fails_setup(tmp_path, caplog):
    platform = _platform(_pdk(tmp_path))
    backend, _ = _setup_backend(
        tmp_path, platform, dict.fromkeys(_CORNERS, "a.sdc"), modes=False
    )

    failed = backend._check_corner_constraints(platform, "26Q2-911")

    assert failed.results["fail_stage"] == "setup"
    assert "per-corner SDC needs an SDC mode per corner" in failed.results["desc"]
    assert "upgrade OpenROAD or give one SDC path" in caplog.text


@pytest.mark.parametrize(
    "stdout, expected",
    [("RB_STA_MODES:yes\n", True), ("RB_STA_MODES:no\n", False)],
)
def test_the_mode_probe_reads_its_answer(tmp_path, monkeypatch, stdout, expected):
    from rtl_buddy.tools import openroad_corners, pnr_openroad

    backend, _ = _setup_backend(tmp_path, _platform(_pdk(tmp_path)), None)
    del backend._probe_sta_modes
    seen = {}

    def _run(cmd, **kwargs):
        seen.update(kwargs)
        return MagicMock(stdout=stdout)

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)

    assert backend._probe_sta_modes() is expected
    assert seen["input"] == openroad_corners.MODES_PROBE_TCL


@pytest.mark.parametrize(
    "outcome, problem",
    [
        (
            subprocess.TimeoutExpired("openroad", 15),
            "it did not finish in 15 s",
        ),
        (OSError("Permission denied"), "it could not be started: Permission denied"),
        (
            MagicMock(
                stdout="OpenROAD 26Q4\n", stderr="Segmentation fault\n", returncode=139
            ),
            "it exited with code 139 without an answer (Segmentation fault)",
        ),
    ],
)
def test_the_mode_probe_says_why_it_has_no_answer(
    tmp_path, monkeypatch, outcome, problem
):
    from rtl_buddy.tools import pnr_openroad

    backend, _ = _setup_backend(tmp_path, _platform(_pdk(tmp_path)), None)
    del backend._probe_sta_modes

    def _run(cmd, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)

    assert backend._probe_sta_modes() == problem


def test_a_probe_without_an_answer_is_tried_once_more(tmp_path):
    platform = _platform(_pdk(tmp_path))
    backend, _ = _setup_backend(tmp_path, platform, _sdc(tmp_path, "top.sdc"))
    answers = ["it did not finish in 15 s", True]
    backend._probe_sta_modes = lambda: answers.pop(0)

    assert backend._check_corner_constraints(platform, "26Q4") is None

    assert not answers
    assert backend._sta_modes is True


def test_a_probe_without_an_answer_twice_fails_setup_naming_openroad(tmp_path, caplog):
    platform = _platform(_pdk(tmp_path))
    backend, _ = _setup_backend(tmp_path, platform, _sdc(tmp_path, "top.sdc"))
    backend.openroad_executable = "/opt/or/bin/openroad"
    calls = []

    def _probe():
        calls.append(True)
        return "it did not finish in 15 s"

    backend._probe_sta_modes = _probe

    failed = backend._check_corner_constraints(platform, "26Q4")

    assert len(calls) == 2
    assert failed.results["fail_stage"] == "setup"
    assert failed.results["desc"] == (
        "could not tell whether /opt/or/bin/openroad gives each corner an SDC "
        "mode: it did not finish in 15 s"
    )
    assert "pnr.corner_modes_unsupported" not in caplog.text
    assert "cannot switch between SDC modes" not in caplog.text
    assert (
        'P&R "demo_pnr": could not tell whether /opt/or/bin/openroad can give '
        "each corner an SDC mode: it did not finish in 15 s"
    ) in caplog.text


def test_the_mode_probe_switches_back_to_an_earlier_mode():
    from rtl_buddy.tools.openroad_corners import MODES_PROBE_TCL

    # 26Q2's `set_mode` cannot return to a mode it made earlier.
    assert "set_mode rb_a\n  set_mode rb_b\n  set_mode rb_a\n" in MODES_PROBE_TCL
    assert '[sta::cmd_mode_name] eq "rb_a"' in MODES_PROBE_TCL
    assert "[info commands define_scene]" in MODES_PROBE_TCL


# ── rendering ──────────────────────────────────────────────────────────────


def _commands(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln and not ln.lstrip().startswith("#")]


def test_each_corner_gets_a_mode_and_a_scene_before_the_netlist(tmp_path):
    macro = tmp_path / "macro.lib"
    macro.write_text("")
    text = _render(tmp_path, _platform(_pdk(tmp_path)), lib_paths=[str(macro)])

    lines = _commands(text)
    reads = [ln for ln in lines if ln.startswith("read_liberty")]
    assert reads == [
        *(f"read_liberty {_lib(tmp_path, c)}" for c in _CORNERS),
        # A macro Liberty is read once and named by every scene.
        f"read_liberty {macro}",
    ]
    scenes = lines.index("set_mode tt")
    assert lines[scenes : scenes + 7] == [
        *(
            line
            for c in _CORNERS
            for line in (
                f"set_mode {c}",
                f"define_scene {c} -mode {c} -liberty {{{_lib(tmp_path, c)} {macro}}}",
            )
        ),
        "set_mode tt",
    ]
    assert lines.index(reads[-1]) < scenes < lines.index("read_verilog $NETLIST")
    assert "define_corners" not in text
    assert "-corner" not in "\n".join(reads)
    assert "{{" not in text


def test_the_sdc_is_read_into_every_mode(tmp_path):
    text = _render(tmp_path, _platform(_pdk(tmp_path)))

    assert (
        'puts ">>> Reading SDC"\n'
        "read_sdc -mode tt $SDC_FILE\n"
        "read_sdc -mode ss $SDC_FILE\n"
        "read_sdc -mode ff $SDC_FILE\n"
        "set_mode tt\n"
    ) in text


def test_a_primary_corner_named_default_stays_the_command_mode(tmp_path):
    # OpenSTA's `read_sdc -mode` does not return to a mode named `default`.
    corners = ["default", "ff"]
    pdk = _pdk(
        tmp_path, corners={c: f"pdk/lib/{c}.lib" for c in [*_CORNERS, "default"]}
    )
    text = _render(tmp_path, _platform(pdk, corners=corners))

    assert (
        "read_sdc -mode default $SDC_FILE\n"
        "read_sdc -mode ff $SDC_FILE\n"
        "set_mode default\n"
    ) in text


def test_a_per_corner_sdc_is_read_into_its_corner_mode(tmp_path):
    sdcs = {c: _sdc(tmp_path, f"{c}.sdc") for c in _CORNERS}
    text = _render(tmp_path, _platform(_pdk(tmp_path)), constraints=sdcs)

    for corner, path in sdcs.items():
        assert f'read_sdc -mode {corner} "{path}"\n' in text
    # The primary corner's SDC is the run's `SDC_FILE`.
    assert f"set SDC_FILE        {sdcs['tt']}\n" in text


def test_a_per_corner_sdc_path_is_quoted_for_tcl(tmp_path):
    odd = tmp_path / 'a $b [c] "d"'
    odd.mkdir()
    sdcs = {c: _sdc(odd, f"{c}.sdc") for c in _CORNERS}
    text = _render(tmp_path, _platform(_pdk(tmp_path)), constraints=sdcs)

    quoted = str(odd).replace("$", "\\$").replace("[", "\\[").replace("]", "\\]")
    quoted = quoted.replace('"', '\\"')
    assert f'read_sdc -mode ss "{quoted}/ss.sdc"\n' in text


def test_max_fanout_and_propagated_clocks_apply_in_every_mode(tmp_path):
    text = _render(tmp_path, _platform(_pdk(tmp_path), max_fanout=20))

    for command in (
        "set_max_fanout 20 [current_design]",
        "set_propagated_clock [all_clocks]",
    ):
        each = "".join(f"set_mode {c}\n{command}\n" for c in _CORNERS) + "set_mode tt\n"
        assert each in text, command
        assert text.count(command) == len(_CORNERS)


def test_the_final_reports_read_the_routed_spef_once_per_corner(tmp_path):
    pdk = _pdk(tmp_path, rcx_rules="pdk/rcx.rules")
    for modes in (True, False):
        text = _render(tmp_path, _platform(pdk), modes=modes)
        assert (
            'puts ">>> Final reports"\n'
            + "".join(
                f"read_spef -corner {c} $OUT_DIR/${{DESIGN}}.routed.spef\n"
                for c in _CORNERS
            )
            + "report_design_area\n"
        ) in text
        assert "read_spef $OUT_DIR" not in text


def test_each_corner_mode_is_written_beside_the_primary_routed_sdc(tmp_path):
    text = _render(tmp_path, _platform(_pdk(tmp_path)))

    assert (
        "write_sdc     $OUT_DIR/${DESIGN}.routed.sdc\n"
        + "".join(
            f"set_mode {c}\nwrite_sdc     $OUT_DIR/${{DESIGN}}.{c}.routed.sdc\n"
            for c in _CORNERS
        )
        + "set_mode tt\n"
    ) in text


def test_without_modes_each_corner_sdc_is_the_shared_mode(tmp_path):
    text = _render(tmp_path, _platform(_pdk(tmp_path)), modes=False)

    assert (
        "write_sdc     $OUT_DIR/${DESIGN}.routed.sdc\n"
        + "".join(
            f"write_sdc     $OUT_DIR/${{DESIGN}}.{c}.routed.sdc\n" for c in _CORNERS
        )
        + "# OpenROAD binary DB"
    ) in text


def test_without_modes_the_corners_share_one_mode_as_before(tmp_path):
    macro = tmp_path / "macro.lib"
    macro.write_text("")
    text = _render(
        tmp_path,
        _platform(_pdk(tmp_path), max_fanout=20),
        modes=False,
        lib_paths=[str(macro)],
    )

    lines = _commands(text)
    assert "define_corners tt ss ff" in lines
    assert [ln for ln in lines if ln.startswith("read_liberty")] == [
        *(f"read_liberty -corner {c} {_lib(tmp_path, c)}" for c in _CORNERS),
        *(f"read_liberty -corner {c} {macro}" for c in _CORNERS),
    ]
    assert 'puts ">>> Reading SDC"\nread_sdc $SDC_FILE\n' in text
    assert text.count("set_max_fanout 20 [current_design]") == 1
    assert text.count("set_propagated_clock [all_clocks]") == 1
    for absent in ("set_mode", "define_scene", "rb::ckpt::write_sdc"):
        assert absent not in text


def test_a_single_corner_run_renders_no_mode(tmp_path):
    text = _render(
        tmp_path, _platform(_pdk(tmp_path), corners=["tt"]), checkpoints=True
    )

    for absent in ("set_mode", "define_scene", "-mode", "-corner", ".tt.routed"):
        assert absent not in text
    assert 'puts ">>> Reading SDC"\nread_sdc $SDC_FILE\n' in text


def test_checkpoints_write_every_mode_into_one_sdc_and_each_alone(tmp_path):
    text = _render(tmp_path, _platform(_pdk(tmp_path)), checkpoints=True)

    proc = text.index("proc rb::ckpt::write_sdc {args} {")
    assert text.index("define_scene ff") < proc < text.index("read_verilog")
    assert "foreach mode {tt ss ff} {" in text[proc:]
    assert " strict 0 modes {tt ss ff}}" in text


def test_checkpoints_of_a_run_without_modes_list_no_corner_sdc(tmp_path):
    for platform, modes in (
        (_platform(_pdk(tmp_path)), False),
        (_platform(_pdk(tmp_path), corners=["tt"]), True),
    ):
        text = _render(tmp_path, platform, modes=modes, checkpoints=True)
        assert " strict 0}" in text
        assert "proc rb::ckpt::write_sdc" not in text


# ── staged sessions, under the stub OpenROAD ───────────────────────────────

# Modes for the stub OpenROAD: each mode keeps the SDC commands it was given; `write_sdc` writes the command mode's; `read_sdc` evaluates a file like OpenSTA's `include_file`, in the mode `-mode` names, and returns to the earlier mode as OpenSTA does. `RB_STUB_REPORT_MODES` prints every mode's commands as the session ends.
_MODES_HARNESS = r"""
set ::stub_mode default
proc set_mode {mode} { set ::stub_mode $mode; return "" }
namespace eval sta {}
proc sta::cmd_mode_name {} { return $::stub_mode }
proc define_scene {args} { return "" }
proc stub_sdc {args} { lappend ::stub_sdc($::stub_mode) [join $args] }
proc set_propagated_clock {args} { stub_sdc set_propagated_clock }
proc set_max_fanout {args} { stub_sdc set_max_fanout [lindex $args 0] }
proc create_clock {args} { stub_sdc create_clock {*}$args }
proc set_output_delay {args} { stub_sdc set_output_delay {*}$args }
proc read_sdc {args} {
    set prev $::stub_mode
    set i [lsearch -exact $args -mode]
    if {$i >= 0} { set ::stub_mode [lindex $args [expr {$i + 1}]] }
    set fh [open [lindex $args end]]
    foreach line [split [read $fh] "\n"] {
        if {[string trim $line] ne ""} { uplevel #0 $line }
    }
    close $fh
    # As `sta::read_sdc_base`: the mode before `-mode` comes back unless it is `default`.
    if {$i >= 0 && $prev ne "default"} { set ::stub_mode $prev }
    rb_stub read_sdc
}
proc write_sdc {args} {
    set fh [open [lindex $args end] w]
    if {[info exists ::stub_sdc($::stub_mode)]} {
        foreach command $::stub_sdc($::stub_mode) { puts $fh $command }
    }
    close $fh
}
rename exit stub_exit
proc exit {{code 0}} {
    foreach mode [lsort [array names ::stub_sdc]] {
        puts "stub: mode $mode: [join $::stub_sdc($mode) {; }]"
    }
    puts "stub: command mode $::stub_mode"
    stub_exit $code
}
"""


def _modes_backend(
    tmp_path, monkeypatch, constraints, corners=("tt", "ff"), stages=True, **kw
):
    backend, artefacts = _backend(tmp_path, monkeypatch, stages=stages, **kw)
    harness = tmp_path / "harness.tcl"
    head, tail = _HARNESS.rsplit("source [lindex $argv 0]", 1)
    harness.write_text(head + _MODES_HARNESS + "source [lindex $argv 0]" + tail)
    pdk = _pdk(tmp_path, corners={c: f"pdk/lib/{c}.lib" for c in {*_CORNERS, *corners}})
    backend.root_cfg.get_pnr_platform_cfg.return_value = _platform(
        pdk, corners=list(corners)
    )
    backend.pnr_cfg = replace(backend.pnr_cfg, constraints=constraints)
    return backend, artefacts


def _mode_lines(log: str) -> dict[str, str]:
    return dict(re.findall(r"^stub: mode (\S+): (.*)$", log, re.MULTILINE))


def test_a_staged_session_restores_every_mode_from_the_checkpoint(
    tmp_path, monkeypatch
):
    sdcs = {
        "tt": _sdc(
            tmp_path, "tt.sdc", "create_clock -period 10 clk\nset_output_delay 1.0 q\n"
        ),
        "ff": _sdc(
            tmp_path, "ff.sdc", "create_clock -period 10 clk\nset_output_delay 0.3 q\n"
        ),
    }
    backend, artefacts = _modes_backend(
        tmp_path, monkeypatch, sdcs, stop_after="global_route"
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    # One checkpoint SDC holds each mode's constraints after its `set_mode`, the primary's last.
    sdc = (run_dir / "01_floorplan.sdc").read_text()
    assert sdc == (
        "set_mode tt\ncreate_clock -period 10 clk\nset_output_delay 1.0 q\n"
        "set_mode ff\ncreate_clock -period 10 clk\nset_output_delay 0.3 q\n"
        "set_mode tt\n"
    )
    # Each mode is also written alone, a plain SDC, and listed with the checkpoint.
    assert (run_dir / "01_floorplan.tt.sdc").read_text() == (
        "create_clock -period 10 clk\nset_output_delay 1.0 q\n"
    )
    assert (run_dir / "01_floorplan.ff.sdc").read_text() == (
        "create_clock -period 10 clk\nset_output_delay 0.3 q\n"
    )
    assert not list(run_dir.glob("*.sdc.*"))
    files = json.loads((run_dir / "manifest.json").read_text())["checkpoints"][
        "floorplan"
    ]["files"]
    assert files["sdc_tt"]["path"].endswith("01_floorplan.tt.sdc")
    assert files["sdc_ff"]["path"].endswith("01_floorplan.ff.sdc")
    # Each later session restores both modes: after CTS with propagated clocks in each.
    place = _mode_lines((run_dir / "02_place.log").read_text())
    assert place == {
        "tt": "create_clock -period 10 clk; set_output_delay 1.0 q",
        "ff": "create_clock -period 10 clk; set_output_delay 0.3 q",
    }
    route = _mode_lines((run_dir / "04_global_route.log").read_text())
    for corner, delay in (("tt", "1.0"), ("ff", "0.3")):
        assert route[corner].startswith(
            f"create_clock -period 10 clk; set_output_delay {delay} q; "
            "set_propagated_clock"
        )
    for name in ("01_floorplan", "02_place", "03_cts", "04_global_route"):
        assert "stub: command mode tt" in (run_dir / f"{name}.log").read_text()
    manifest = (run_dir / "manifest.json").read_text()
    assert "tt.sdc" in manifest and "ff.sdc" in manifest


def test_a_primary_corner_named_default_is_the_command_mode_after_the_sdc(
    tmp_path, monkeypatch
):
    sdc = _sdc(tmp_path, "top.sdc", "create_clock -period 10 clk\n")
    backend, artefacts = _modes_backend(
        tmp_path, monkeypatch, sdc, corners=("default", "ff"), stop_after="floorplan"
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    log = (run_dir / "01_floorplan.log").read_text()
    assert "stub: command mode default" in log
    assert (run_dir / "01_floorplan.default.sdc").is_file()


def test_the_checkpoint_sdc_writer_returns_to_the_mode_it_was_called_in(tmp_path):
    from rtl_buddy.tools.pnr_openroad import _mode_checkpoint_sdc_tcl

    harness = tmp_path / "harness.tcl"
    head, tail = _HARNESS.rsplit("source [lindex $argv 0]", 1)
    harness.write_text(head + _MODES_HARNESS + "source [lindex $argv 0]" + tail)
    script = tmp_path / "write.tcl"
    script.write_text(
        _mode_checkpoint_sdc_tcl(["tt", "ff"])
        + "\nset_mode tt\ncreate_clock tt_clk\nset_mode ff\ncreate_clock ff_clk\n"
        + f"rb::ckpt::write_sdc -no_timestamp {tmp_path}/ck.sdc\n"
        + 'puts "after: [sta::cmd_mode_name]"\n'
    )

    out = _REAL_RUN(
        _tcl_command(harness, str(script)), capture_output=True, text=True, check=True
    ).stdout

    assert "after: ff" in out
    assert (tmp_path / "ck.tt.sdc").read_text() == "create_clock tt_clk\n"
    assert (tmp_path / "ck.ff.sdc").read_text() == "create_clock ff_clk\n"
    assert (tmp_path / "ck.sdc").read_text() == (
        "set_mode tt\ncreate_clock tt_clk\nset_mode ff\ncreate_clock ff_clk\n"
        "set_mode tt\n"
    )


def test_a_checkpoint_without_a_corner_sdc_is_not_written(tmp_path):
    from rtl_buddy.tools import pnr_checkpoints

    harness = tmp_path / "harness.tcl"
    harness.write_text(_HARNESS)
    script = tmp_path / "ckpt.tcl"
    script.write_text(
        pnr_checkpoints._procs()
        + f"\nrb::ckpt::arm {tmp_path} {tmp_path}/progress.jsonl {{}} "
        "{anchors {} wanted {floorplan} order {floorplan 01} routed {} "
        "reports {} state {} account 0 strict 1 modes {tt ff}}\n"
        # An SDC writer that writes no corner's file.
        "proc write_sdc {args} { close [open [lindex $args end] w] }\n"
        "set DESIGN top\nset OUT_DIR .\n"
        "catch {rb::ckpt::write_checkpoint 01 floorplan 1} err\n"
        'puts "ERR: $err"\n'
    )

    out = _REAL_RUN(
        _tcl_command(harness, str(script)), capture_output=True, text=True, check=True
    ).stdout

    assert (
        f"ERR: checkpoint 01_floorplan not written: {tmp_path}/01_floorplan.tt.sdc not written"
        in out
    )


def test_resume_refuses_a_changed_per_corner_sdc(tmp_path, monkeypatch):
    sdcs = {c: _sdc(tmp_path, f"{c}.sdc") for c in ("tt", "ff")}
    backend, artefacts = _modes_backend(tmp_path, monkeypatch, sdcs, stop_after="place")
    assert backend.run().results["result"] == "PASS"
    (first,) = _runs(artefacts)
    Path(sdcs["ff"]).write_text("create_clock -period 5 clk\n")

    backend, _ = _modes_backend(
        tmp_path, monkeypatch, sdcs, resume=first.name, resume_from="cts"
    )
    res = backend.run()

    assert res.results["fail_stage"] == "resume"
    assert "input sdc differs" in res.results["desc"]
    assert "ff.sdc" in res.results["desc"]


def test_a_staged_multi_corner_run_resumes(tmp_path, monkeypatch):
    sdcs = {c: _sdc(tmp_path, f"{c}.sdc") for c in ("tt", "ff")}
    backend, artefacts = _modes_backend(tmp_path, monkeypatch, sdcs, stop_after="place")
    assert backend.run().results["result"] == "PASS"
    (first,) = _runs(artefacts)

    backend, _ = _modes_backend(
        tmp_path, monkeypatch, sdcs, resume=first.name, resume_from="cts"
    )
    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    second = next(r for r in _runs(artefacts) if r != first)
    route = _mode_lines((second / "04_global_route.log").read_text())
    assert set(route) == {"tt", "ff"}


def test_a_hardened_block_digests_a_one_corner_mapping_as_its_path(tmp_path):
    from rtl_buddy.tools.pnr_abstract import abstract_config

    platform = _platform(_pdk(tmp_path), corners=["tt"])
    sdc = _sdc(tmp_path, "block.sdc")

    plain = abstract_config(_cfg(tmp_path, constraints=sdc), platform)
    mapped = abstract_config(_cfg(tmp_path, constraints={"tt": sdc}), platform)

    assert mapped == plain


# ── real OpenROAD ──────────────────────────────────────────────────────────

_OPENROAD = shutil.which("openroad")

_TINY_LEF = """\
VERSION 5.8 ;
UNITS
  DATABASE MICRONS 1000 ;
END UNITS
LAYER m1
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
  PITCH 0.2 ;
  WIDTH 0.1 ;
END m1
END LIBRARY
"""


def _openroad(tmp_path, name, tcl, stdin=False):
    script = tmp_path / name
    script.write_text(tcl)
    cmd = [_OPENROAD, "-no_init", "-exit"]
    r = subprocess.run(
        cmd if stdin else [*cmd, str(script)],
        input=tcl if stdin else None,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return r.stdout + r.stderr


def _output_delay(path) -> float:
    (value,) = re.findall(r"^set_output_delay (\S+)", Path(path).read_text(), re.M)
    return round(float(value), 6)


@pytest.mark.skipif(_OPENROAD is None, reason="needs openroad on PATH")
def test_checkpoint_sdc_modes_round_trip_on_real_opensta(tmp_path):
    from rtl_buddy.tools import openroad_corners
    from rtl_buddy.tools.pnr_openroad import _mode_checkpoint_sdc_tcl

    answer = _openroad(tmp_path, "probe.tcl", openroad_corners.MODES_PROBE_TCL, True)
    assert (openroad_corners.MODES_PROBE_YES in answer) != (
        openroad_corners.MODES_PROBE_NO in answer
    ), answer
    if openroad_corners.MODES_PROBE_YES not in answer:
        pytest.skip("this OpenROAD has no SDC mode per corner")

    corners = {"tt": [str(tmp_path / "tt.lib")], "ff": [str(tmp_path / "ff.lib")]}
    for corner, (lib,) in corners.items():
        Path(lib).write_text(f'library ({corner}) {{ time_unit : "1ns" ; }}\n')
        _sdc(
            tmp_path,
            f"{corner}.sdc",
            "create_clock -name clk -period 10 [get_ports clk]\n"
            f"set_output_delay {1.0 if corner == 'tt' else 0.3} -clock clk "
            "[get_ports q]\n",
        )
    (tmp_path / "tech.lef").write_text(_TINY_LEF)
    (tmp_path / "top.v").write_text(
        "module top (clk, q);\n  input clk;\n  output q;\nendmodule\n"
    )

    def _session(name, modes, body):
        head = [f"read_liberty {lib}" for (lib,) in corners.values()]
        if modes:
            head += openroad_corners.scenes_tcl(corners, [])
            head.append(_mode_checkpoint_sdc_tcl(list(corners)))
        head += ["read_lef tech.lef", "read_verilog top.v", "link_design top"]
        return _openroad(tmp_path, name, "\n".join([*head, body, "exit", ""]))

    log = _session(
        "write.tcl",
        True,
        "read_sdc -mode tt tt.sdc\nread_sdc -mode ff ff.sdc\nset_mode ff\n"
        "rb::ckpt::write_sdc -no_timestamp -digits 9 ck.sdc\n"
        'puts "RB_MODE:[sta::cmd_mode_name]"',
    )
    assert "RB_MODE:ff" in log, log
    assert _output_delay(tmp_path / "ck.tt.sdc") == 1.0
    assert _output_delay(tmp_path / "ck.ff.sdc") == 0.3

    log = _session(
        "read.tcl",
        True,
        "read_sdc ck.sdc\n"
        'puts "RB_MODE:[sta::cmd_mode_name]"\n'
        "foreach m {tt ff} { set_mode $m; write_sdc -no_timestamp back.$m.sdc }",
    )
    assert "RB_MODE:tt" in log, log
    assert _output_delay(tmp_path / "back.tt.sdc") == 1.0
    assert _output_delay(tmp_path / "back.ff.sdc") == 0.3

    # A corner's own file reads into a session without modes.
    log = _session("plain.tcl", False, "read_sdc ck.ff.sdc\nwrite_sdc plain.sdc")
    assert _output_delay(tmp_path / "plain.sdc") == 0.3, log
