"""The stub OpenROAD the P&R backend tests run a generated `pnr.tcl` under, and an `OpenRoadPnr` on it (`_backend`).

The flow runs under a plain Tcl interpreter with every OpenROAD command stubbed (`_HARNESS`). A stub can fail, or SIGKILL its interpreter, at a named command to stage routing failures and wall-limit kills.
"""

import functools
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from rtl_buddy.config.pdk import PdkConfig, PdkConfigFile, PdkPinLayersFile
from rtl_buddy.config.pnr import CHECKPOINT_STAGES, PnrConfig, PnrFloorplan
from rtl_buddy.config.pnr_platform import (
    PnrPlatformConfig,
    PnrPlatformConfigFile,
    PnrRoutingLayersFile,
)

DESIGN = "demo_top"

# Stub OpenROAD: every command the flow calls, as a Tcl proc or through `unknown`.
# Writers put a recognisable file at the given path (a DEF with its DESIGN statement).
# `RB_STUB_FAIL=<cmd>` raises an OpenROAD-style error in that command, `STUB-0001` or
# `RB_STUB_FAIL_CODE`;
# `RB_STUB_KILL=<cmd>` SIGKILLs the interpreter on entry;
# `RB_STUB_EXIT=<cmd>` exits 0 on entry, as a flow that ends early by accident;
# `RB_STUB_RULES=1` gives the database global-connection rules, so `rb::global_connect`
# calls `global_connect`. `global_connect` and the database writers log a
# `stub: ...` line, which orders them in the log;
# `RB_STUB_DONE_EARLY=<cmd>` prints the flow's end marker in that command, which returns;
# `RB_STUB_DRC=<n>` makes `detailed_route` report n DRC violations and write a maze log;
# `RB_STUB_ERRLINE=<cmd>` prints an OpenROAD `[ERROR` line in that command, which returns.
# `RB_STUB_CKPT_FAIL=<NN_stage>` makes `write_db` of that checkpoint fail without an `[ERROR` line.
# `RB_STUB_NO_ROWS=1` leaves the core without rows; it has one otherwise.
# `RB_STUB_ANTENNAS=<n>` makes `check_antennas` report n violating nets, one fewer after each
# `repair_antennas`, which returns whether there were any. Its `-report_file` names the count
# and the session's script.
# `read_guides` and `read_global_route_segments` fail on a missing file. Segments written
# after a `read_guides` in the session are refused on reading, as OpenROAD 26Q4 refuses
# their single-point segments (GRT-0265).
_HARNESS = r"""
# `clock format` loads msgcat through the auto-loader, which the catch-all
# `unknown` below would swallow; load it first.
clock format [clock seconds]
proc unknown {args} {
    global env
    if {[lindex $args 1] eq "getGlobalConnects" && [info exists env(RB_STUB_RULES)]} {
        return rule
    }
    # The core has one row unless `RB_STUB_NO_ROWS` is set.
    if {[lindex $args 1] eq "getRows" && ![info exists env(RB_STUB_NO_ROWS)]} {
        return row
    }
    return ""
}
namespace eval ord {}
proc ord::microns_to_dbu {x} { return [expr {int($x * 1000)}] }
proc rb_stub {name args} {
    global env
    if {[info exists env(RB_STUB_KILL)] && $env(RB_STUB_KILL) eq $name} {
        puts "stub: killing in $name"
        flush stdout
        exec kill -9 [pid]
    }
    if {[info exists env(RB_STUB_EXIT)] && $env(RB_STUB_EXIT) eq $name} {
        exit 0
    }
    if {[info exists env(RB_STUB_FAIL)] && $env(RB_STUB_FAIL) eq $name} {
        set code [expr {[info exists env(RB_STUB_FAIL_CODE)] ? $env(RB_STUB_FAIL_CODE) : "STUB-0001"}]
        puts "\[ERROR $code\] $name failed: \"quoted\"\nsecond line"
        error "$code $name failed: \"quoted\"\nsecond line"
    }
    if {[info exists env(RB_STUB_DONE_EARLY)] && $env(RB_STUB_DONE_EARLY) eq $name} {
        puts ">>> DONE"
    }
    if {[info exists env(RB_STUB_ERRLINE)] && $env(RB_STUB_ERRLINE) eq $name} {
        puts "\[ERROR STUB-0002\] $name complained but returned"
    }
    return ""
}
foreach cmd {link_design read_sdc initialize_floorplan place_pins
             insert_tiecells pdngen global_placement repair_tie_fanout
             repair_design
             detailed_placement improve_placement optimize_mirroring
             repair_clock_inverters clock_tree_synthesis repair_clock_nets
             repair_timing
             check_placement global_route detailed_route filler_placement} {
    proc $cmd {args} [list rb_stub $cmd]
}
set rb_antennas [expr {[info exists env(RB_STUB_ANTENNAS)] ? $env(RB_STUB_ANTENNAS) : 0}]
proc repair_antennas {args} {
    rb_stub repair_antennas
    puts "stub: repair_antennas"
    if {$::rb_antennas > 0} {
        incr ::rb_antennas -1
        return 1
    }
    return 0
}
proc check_antennas {args} {
    rb_stub check_antennas
    set i [lsearch -exact $args -report_file]
    if {$i >= 0} {
        rb_write [lindex $args [expr {$i + 1}]] \
            "antennas $::rb_antennas [file tail [lindex $::argv 0]]"
    }
    return $::rb_antennas
}
proc detailed_route {args} {
    global env
    if {[info exists env(RB_STUB_DRC)]} {
        set fh [open [lindex $args [expr {[lsearch -exact $args -output_drc] + 1}]] w]
        for {set i 0} {$i < $env(RB_STUB_DRC)} {incr i} {
            puts $fh "violation type: Short\n\tsrcs: net:a$i net:b$i\n\tbbox = ($i.0, 0.0) - ($i.1, 0.1) on Layer met1"
        }
        close $fh
        set fh [open [lindex $args [expr {[lsearch -exact $args -output_maze] + 1}]] w]
        puts $fh "-output_maze 0"
        close $fh
    }
    rb_stub detailed_route
    puts "stub: detailed_route"
}
proc rb_write {path text} {
    set fh [open $path w]
    puts $fh $text
    close $fh
}
proc global_connect {} { puts "stub: global_connect" }
proc write_def {path} {
    global DESIGN
    puts "stub: write_def [file tail $path]"
    rb_write $path "DESIGN $DESIGN ;\nEND DESIGN"
}
proc write_db {path} {
    global env
    if {[info exists env(RB_STUB_CKPT_FAIL)]
        && [file rootname [file tail $path]] eq $env(RB_STUB_CKPT_FAIL)} {
        error "cannot write $path"
    }
    rb_stub write_db
    puts "stub: write_db [file tail $path]"
    rb_write $path "odb"
}
proc write_sdc {args} { rb_write [lindex $args end] "sdc" }
# The top's ports in OpenROAD's order: a session that read the database has the supply
# ports before the buses, the session that built the grid after them.
proc read_db {args} { set ::rb_read_db 1 }
proc rb_ports {pwr} {
    set ports [expr {[info exists ::rb_read_db] ? {clk VDD VSS d} : {clk d VDD VSS}}]
    if {$pwr} { return $ports }
    return [lsearch -all -inline -not -glob $ports V*]
}
proc write_verilog {args} {
    set ports [rb_ports [expr {"-include_pwr_gnd" in $args}]]
    set dcls {clk { input clk;} d { input [1:0] d;} VDD { inout VDD;} VSS { inout VSS;}}
    set text "module demo_top ([join $ports ",\n    "]);\n"
    foreach port $ports { append text [dict get $dcls $port] "\n" }
    rb_write [lindex $args end] "$text\nendmodule"
}
proc write_guides {path} { rb_write $path "guides" }
proc write_global_route_segments {path} {
    rb_write $path [expr {[info exists ::rb_read_guides] ? "segments from guides" : "segments"}]
}
proc rb_read {cmd path} {
    if {![file exists $path]} { error "$cmd: cannot open $path" }
    puts "stub: $cmd [file tail $path]"
}
proc read_guides {path} {
    rb_read read_guides $path
    set ::rb_read_guides 1
}
proc read_global_route_segments {path} {
    rb_read read_global_route_segments $path
    set fh [open $path]
    set text [string trim [read $fh]]
    close $fh
    if {$text eq "segments from guides"} {
        puts "\[ERROR GRT-0265\] Segment 2 of net n1 is not a horizontal/vertical line or via."
        error "GRT-0265"
    }
}
proc write_abstract_lef {args} { rb_write [lindex $args end] "MACRO demo_top" }
proc write_timing_model {args} {
    set text "library (demo_top) \{\n  cell (\"demo_top\") \{\n"
    foreach port [rb_ports 1] {
        if {$port eq "d"} {
            append text "    bus(\"d\") \{\n    pin(\"d\[0\]\") \{\n    \}\n    \}\n"
        } else {
            append text "    pin(\"$port\") \{\n      direction : input;\n    \}\n"
        }
    }
    rb_write [lindex $args end] "$text  \}\n\}"
}
source [lindex $argv 0]
"""

# Embedded-Tcl fallback when `tclsh` is missing or too old: the same harness in the
# `tkinter` interpreter, in a child process.
_EMBEDDED_DRIVER = """
import os, sys, tkinter
interp = tkinter.Tcl()

def _exit(code=0):
    # tkinter's interpreter has no `exit`. As tclsh's, it flushes every channel and ends
    # the process at once, so no trace sees the command that called it return.
    interp.eval("foreach rb_ch [file channels] { catch {flush $rb_ch} }")
    os._exit(int(code))

interp.createcommand("exit", _exit)
interp.eval('set argv [list {%s}]' % sys.argv[2])
try:
    interp.eval(open(sys.argv[1]).read())
except tkinter.TclError as e:
    sys.stderr.write(str(e) + "\\n")
    sys.stderr.flush()
    _exit(1)
_exit(0)
"""


# Looked up at import: backend tests patch the shared `shutil.which` to fake OpenROAD.
_TCLSH = shutil.which("tclsh")
_REAL_RUN = subprocess.run

# OpenROAD embeds Tcl 8.6, and the flow uses 8.6 commands (`lmap` in `hook_commands.tcl`).
_TCL_MIN = (8, 6)


def _tcl_version(cmd: list[str], script: str) -> tuple[int, ...] | None:
    """The Tcl version `cmd` reports when it runs `script`, or None when it cannot."""
    try:
        done = _REAL_RUN(cmd, input=script, capture_output=True, text=True)
    except OSError:
        return None
    match = re.fullmatch(r"(\d+)\.(\d+)", done.stdout.strip())
    if done.returncode != 0 or match is None:
        return None
    return tuple(int(part) for part in match.groups())


@functools.cache
def _tcl_interpreter() -> tuple[list[str] | None, str]:
    """The command that runs the stub harness, and why there is none: `tclsh` when it is Tcl 8.6 or newer, else the interpreter `tkinter` embeds when that is."""
    found = []
    if _TCLSH:
        version = _tcl_version([_TCLSH], "puts [info tclversion]\n")
        if version is not None and version >= _TCL_MIN:
            return [_TCLSH], ""
        found.append(f"tclsh {'.'.join(map(str, version or ())) or 'unusable'}")
    probe = "import sys, tkinter; print(tkinter.Tcl().eval(sys.stdin.read()))"
    version = _tcl_version([sys.executable, "-c", probe], "info tclversion")
    if version is not None and version >= _TCL_MIN:
        return [sys.executable, "-c", _EMBEDDED_DRIVER], ""
    found.append(f"tkinter Tcl {'.'.join(map(str, version or ())) or 'unusable'}")
    need = ".".join(map(str, _TCL_MIN))
    return None, f"the P&R flow needs Tcl {need} or newer; found {', '.join(found)}"


def _tcl_command(harness: Path, script: str) -> list[str]:
    command, why = _tcl_interpreter()
    if command is None:  # pragma: no cover - depends on the machine
        pytest.skip(why)
    return [*command, str(harness), script]


def _pdk(tmp_path, **changes):
    fields = {
        "name": "nangate45",
        "pin_layers": PdkPinLayersFile(horizontal="metal3", vertical="metal2"),
        "site": "FreePDK45_38x28_10R_NP_162NW_34O",
        "corners": {"typ": "pdk/lib/typ.lib"},
        "tech_lef": "pdk/lef/tech.lef",
        "macro_lef": "pdk/lef/cells.lef",
        "tie_hi": "LOGIC1_X1/Z",
        "tie_lo": "LOGIC0_X1/Z",
        "fill_cells": ["FILLCELL_X1"],
        "klayout_tech": "pdk/klayout/tech.lyt",
        "cell_gds": "pdk/gds/cells.gds",
    }
    return PdkConfig(
        PdkConfigFile(**{**fields, **changes}), str(tmp_path / "root_config.yaml")
    )


def _platform(pdk, signal="metal2-metal6", **platform):
    return PnrPlatformConfig(
        PnrPlatformConfigFile(
            name="nangate45_typ",
            pdk="nangate45",
            cts_buffer="BUF",
            routing_layers=PnrRoutingLayersFile(signal=signal, clock="metal4"),
            **platform,
        ),
        lambda _name: pdk,
    )


def _write_synth_config(tmp_path):
    (tmp_path / "models.yaml").write_text(
        dedent(f"""\
        rtl-buddy-filetype: model_config
        models:
          - name: "{DESIGN}"
            filelist: []
        """)
    )
    (tmp_path / "synth.yaml").write_text(
        dedent("""\
        rtl-buddy-filetype: synth_config
        syntheses:
          - name: "demo_synth"
            desc: "demo"
            model: "demo_top"
            model_path: "models.yaml"
            tool: "openroad"
            reglvl: 0
        """)
    )


def _pnr_cfg(tmp_path, checkpoints=CHECKPOINT_STAGES, *, stages=False):
    sdc = tmp_path / "constraints.sdc"
    sdc.write_text("create_clock -period 1 [get_ports clk]\n")
    return PnrConfig(
        name="demo_pnr",
        desc="demo",
        tool="openroad",
        synth_name="demo_synth",
        synth_suite_path=str(tmp_path / "synth.yaml"),
        constraints=str(sdc),
        platform="nangate45_typ",
        floorplan=PnrFloorplan(utilization=0.55, aspect=1.0, core_margin=2.0),
        _reglvl=0,
        tool_overrides=None,
        checkpoints=CHECKPOINT_STAGES if stages else checkpoints,
        stages=stages,
    )


#: What the stub sessions of a staged run report as their resources.
STUB_RESOURCES = {"wall_s": 1.5, "cpu_s": 2.25, "peak_rss_kb": 4096}


def _backend(
    tmp_path,
    monkeypatch,
    *,
    checkpoints=CHECKPOINT_STAGES,
    env=None,
    stages=False,
    pnr_cfg=None,
    pdk_fields=None,
    **options,
):
    """An `OpenRoadPnr` whose OpenROAD is the stub harness under Tcl.

    `pdk_fields` are extra `PdkConfigFile` fields (the PDK Tcl hooks, `pdn_config`); `options` are `OpenRoadPnr` keywords (`stop_after`, `resume`, `resume_from`).
    """
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    _write_synth_config(tmp_path)
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/usr/bin/openroad")
    pdk = _pdk(tmp_path, **(pdk_fields or {}))
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = _platform(pdk)
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=pnr_cfg or _pnr_cfg(tmp_path, checkpoints, stages=stages),
        suite_dir=str(tmp_path),
        root_cfg=root_cfg,
        **options,
    )
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: "26Q2-stub")
    monkeypatch.setattr(backend, "_has_tcl_command", lambda _c: True)
    monkeypatch.setattr(backend, "_probe_sta_modes", lambda: True)
    harness = tmp_path / "harness.tcl"
    harness.write_text(_HARNESS)

    def _run(cmd, **kwargs):
        cmd = list(cmd)
        done = _REAL_RUN(
            _tcl_command(harness, cmd[-1]),
            stdout=kwargs["stdout"],
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            env={**os.environ, **(env or {})},
        )
        return subprocess.CompletedProcess(cmd, done.returncode, None, done.stderr)

    def _session(cmd, *, stdout, env, cwd):
        assert cwd == backend.artefact_dir
        done = _run(cmd, stdout=stdout)
        return (
            done.returncode,
            done.stderr,
            {**STUB_RESOURCES, "measured_by": "process"},
        )

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)
    monkeypatch.setattr(pnr_openroad, "_run_session", _session)
    return backend, Path(backend.artefact_dir)


def _events(run_dir) -> list[dict]:
    lines = Path(run_dir, "progress.jsonl").read_text().splitlines()
    # Every line is a JSON object, including one whose error message has quotes and a
    # newline.
    return [json.loads(line) for line in lines]


def _runs(artefacts: Path) -> list[Path]:
    root = artefacts / "checkpoints"
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
