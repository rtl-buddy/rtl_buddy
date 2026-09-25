"""Tests for P&R stage checkpoints and progress (#653).

The flow side is exercised for real: the generated `pnr.tcl` is run under
a plain Tcl interpreter with every OpenROAD command stubbed (`_HARNESS`),
so the traces, the checkpoint writes and the JSON-lines events are the
ones the rendered script really produces. A stub can be told to fail —
or to SIGKILL its own interpreter — at a named command, which is how a
routing failure and a wall-limit kill are staged.

What these pin, beyond the issue's acceptance list:

* a run without `checkpoints:` renders the flow byte-for-byte as before;
* checkpoints are never `*.routed.*` and never at a path `rb power` or a
  plain `rb pnr-export` reads;
* every run gets its own directory; `latest` is retired up front by every
  run and re-pointed only by one that launched OpenROAD with checkpoints;
* `rb pnr-export --checkpoint` writes below the checkpoint and labels the
  export as not final, with the congestion grid unavailable (not zero).
"""

import json
import os
import shutil
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from rtl_buddy.config.pdk import PdkConfig, PdkConfigFile
from rtl_buddy.config.pnr import (
    CHECKPOINT_STAGES,
    PnrConfig,
    PnrConfigFile,
    PnrFloorplan,
    PnrSuiteConfig,
)
from rtl_buddy.config.pnr_platform import (
    PnrPlatformConfig,
    PnrPlatformConfigFile,
    PnrRoutingLayersFile,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_checkpoints

DESIGN = "demo_top"

#: Stub OpenROAD: every command the flow calls, as a Tcl proc or through
#: `unknown`. The writers put a recognisable file at the path they are
#: given (a DEF with its DESIGN statement, so the export's design check
#: passes). `RB_STUB_FAIL=<cmd>` makes that command raise an OpenROAD-style
#: error; `RB_STUB_KILL=<cmd>` SIGKILLs the interpreter on entry to it.
_HARNESS = r"""
# `clock format` loads msgcat through the auto-loader, which the catch-all
# `unknown` below would swallow; load it first.
clock format [clock seconds]
proc unknown {args} { return "" }
namespace eval ord {}
proc ord::microns_to_dbu {x} { return [expr {int($x * 1000)}] }
proc rb_stub {name args} {
    global env
    if {[info exists env(RB_STUB_KILL)] && $env(RB_STUB_KILL) eq $name} {
        puts "stub: killing in $name"
        flush stdout
        exec kill -9 [pid]
    }
    if {[info exists env(RB_STUB_FAIL)] && $env(RB_STUB_FAIL) eq $name} {
        puts "\[ERROR STUB-0001\] $name failed: \"quoted\"\nsecond line"
        error "STUB-0001 $name failed: \"quoted\"\nsecond line"
    }
    return ""
}
foreach cmd {link_design read_sdc initialize_floorplan place_pins
             insert_tiecells pdngen global_placement repair_design
             detailed_placement clock_tree_synthesis repair_timing
             check_placement global_route detailed_route filler_placement} {
    proc $cmd {args} [list rb_stub $cmd]
}
proc rb_write {path text} {
    set fh [open $path w]
    puts $fh $text
    close $fh
}
proc write_def {path} { global DESIGN; rb_write $path "DESIGN $DESIGN ;\nEND DESIGN" }
proc write_db {path} { rb_write $path "odb" }
proc write_sdc {path} { rb_write $path "sdc" }
proc write_verilog {path} { rb_write $path "module m(); endmodule" }
proc write_guides {path} { rb_write $path "guides" }
proc write_global_route_segments {path} { rb_write $path "segments" }
source [lindex $argv 0]
"""

#: The embedded-Tcl fallback when there is no `tclsh`: the same harness in
#: the interpreter `tkinter` carries, in a child process (#641).
_EMBEDDED_DRIVER = """
import sys, tkinter
interp = tkinter.Tcl()
# tkinter's interpreter has no `exit`; the flow ends with one.
interp.eval('proc exit {{code 0}} { error "RB_EXIT $code" }')
interp.eval('set argv [list {%s}]' % sys.argv[2])
try:
    interp.eval(open(sys.argv[1]).read())
except tkinter.TclError as e:
    if str(e).startswith("RB_EXIT "):
        sys.exit(int(str(e).split()[1]))
    sys.stderr.write(str(e) + "\\n")
    sys.exit(1)
"""


#: Looked up at import: the backend tests patch `shutil.which` (it is one
#: module, shared) to pretend OpenROAD is installed.
_TCLSH = shutil.which("tclsh")
_REAL_RUN = subprocess.run


def _tcl_command(harness: Path, script: str) -> list[str]:
    tclsh = _TCLSH
    if tclsh:
        return [tclsh, str(harness), script]
    probe = _REAL_RUN(
        [sys.executable, "-c", "import tkinter; tkinter.Tcl()"],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:  # pragma: no cover - depends on the machine
        pytest.skip("no Tcl interpreter (tclsh or tkinter) available")
    return [sys.executable, "-c", _EMBEDDED_DRIVER, str(harness), script]


def _pdk(tmp_path):
    return PdkConfig(
        PdkConfigFile(
            name="nangate45",
            site="FreePDK45_38x28_10R_NP_162NW_34O",
            corners={"typ": "pdk/lib/typ.lib"},
            tech_lef="pdk/lef/tech.lef",
            macro_lef="pdk/lef/cells.lef",
            tie_hi="LOGIC1_X1/Z",
            tie_lo="LOGIC0_X1/Z",
            fill_cells=["FILLCELL_X1"],
            klayout_tech="pdk/klayout/tech.lyt",
            cell_gds="pdk/gds/cells.gds",
        ),
        str(tmp_path / "root_config.yaml"),
    )


def _platform(pdk):
    return PnrPlatformConfig(
        PnrPlatformConfigFile(
            name="nangate45_typ",
            pdk="nangate45",
            cts_buffer="BUF",
            routing_layers=PnrRoutingLayersFile(signal="metal2-metal6", clock="metal4"),
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


def _pnr_cfg(tmp_path, checkpoints=CHECKPOINT_STAGES):
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
        checkpoints=checkpoints,
    )


def _backend(tmp_path, monkeypatch, *, checkpoints=CHECKPOINT_STAGES, env=None):
    """An `OpenRoadPnr` whose OpenROAD is the stub harness under Tcl."""
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    _write_synth_config(tmp_path)
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: "/usr/bin/openroad")
    pdk = _pdk(tmp_path)
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = _platform(pdk)
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_pnr_cfg(tmp_path, checkpoints),
        suite_dir=str(tmp_path),
        root_cfg=root_cfg,
    )
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: "26Q2-stub")
    harness = tmp_path / "harness.tcl"
    harness.write_text(_HARNESS)

    def _run(cmd, **kwargs):
        cmd = list(cmd)
        log = cmd[cmd.index("-log") + 1]
        with open(log, "w") as out:
            done = _REAL_RUN(
                _tcl_command(harness, cmd[-1]),
                stdout=out,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                env={**os.environ, **(env or {})},
            )
        return subprocess.CompletedProcess(cmd, done.returncode, None, done.stderr)

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)
    return backend, Path(backend.artefact_dir)


def _events(run_dir) -> list[dict]:
    lines = Path(run_dir, "progress.jsonl").read_text().splitlines()
    # Every line is a JSON object — including the one carrying an error
    # message with quotes and a newline in it.
    return [json.loads(line) for line in lines]


def _runs(artefacts: Path) -> list[Path]:
    root = artefacts / "checkpoints"
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())


_ROUTED = ("demo_top.def", "demo_top.routed.odb", "demo_top.routed.sdc")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _cfg_file(**kw):
    return PnrConfigFile(
        name="r", desc="r", synth="s", synth_path="synth.yaml", platform="p", **kw
    )


@pytest.mark.parametrize(
    "value, expected",
    [
        (False, None),
        (True, CHECKPOINT_STAGES),
        ("cts", ("cts",)),
        # Flow order, whatever order the list is written in.
        (["global_route", "floorplan"], ("floorplan", "global_route")),
        ([], ()),
    ],
)
def test_checkpoints_key_normalises_to_stages_in_flow_order(tmp_path, value, expected):
    cfg = _cfg_file(checkpoints=value).initialise(str(tmp_path))
    assert cfg.get_checkpoints() == expected


def test_checkpoints_key_defaults_to_off(tmp_path):
    assert _cfg_file().initialise(str(tmp_path)).get_checkpoints() is None


def test_checkpoints_key_rejects_an_unknown_stage(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="'detail_route'"):
        _cfg_file(checkpoints=["cts", "detail_route"]).initialise(str(tmp_path))


def test_checkpoints_key_loads_from_yaml(tmp_path):
    pnr_yaml = tmp_path / "pnr.yaml"
    pnr_yaml.write_text(
        dedent("""\
        rtl-buddy-filetype: pnr_config
        runs:
          - name: all
            desc: d
            synth: s
            synth-path: synth.yaml
            platform: p
            checkpoints: true
          - name: one
            desc: d
            synth: s
            synth-path: synth.yaml
            platform: p
            checkpoints: global_route
        """)
    )
    suite = PnrSuiteConfig(str(pnr_yaml))
    assert suite.get_runs("all")[0].get_checkpoints() == CHECKPOINT_STAGES
    assert suite.get_runs("one")[0].get_checkpoints() == ("global_route",)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render(tmp_path, checkpoints):
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    _write_synth_config(tmp_path)
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_pnr_cfg(tmp_path, checkpoints),
        suite_dir=str(tmp_path),
        root_cfg=MagicMock(),
    )
    if checkpoints is not None:
        backend._ckpt_run_dir = str(tmp_path / "ck" / "run")
    platform = _platform(_pdk(tmp_path))
    return Path(backend._write_script(platform, backend.pnr_cfg.get_floorplan()))


def test_flow_without_checkpoints_renders_the_template_unchanged(tmp_path):
    """No `checkpoints:` key, no trace of the feature: the placeholder sits
    on what was a blank line, so the flow is byte-identical to the one the
    template rendered before it existed."""
    text = _render(tmp_path, None).read_text()
    assert 'file mkdir $OUT_DIR\n\nputs ">>> Reading Liberty + LEF"' in text
    assert "rb::ckpt" not in text
    assert "progress.jsonl" not in text
    assert "trace add" not in text


def test_flow_with_checkpoints_arms_only_the_requested_stages(tmp_path):
    text = _render(tmp_path, ("cts", "global_route")).read_text()
    arm = next(line for line in text.splitlines() if line.startswith("rb::ckpt::arm"))
    assert "{cts {03 global_route enter}}" in arm
    assert "{global_route {04 global_route leave}}" in arm
    assert "{floorplan " not in arm
    assert '"' + str(tmp_path / "ck" / "run" / "progress.jsonl") + '"' in arm
    # Armed before the first flow command, so every step is traced.
    assert text.index("rb::ckpt::arm") < text.index("read_liberty $LIBERTY")


# ---------------------------------------------------------------------------
# Runs under the stub OpenROAD
# ---------------------------------------------------------------------------


def test_successful_run_writes_every_checkpoint_and_a_complete_manifest(
    tmp_path, monkeypatch
):
    backend, artefacts = _backend(tmp_path, monkeypatch)

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    assert os.readlink(artefacts / "checkpoints" / "latest") == run_dir.name
    assert res.results["checkpoint_dir"] == str(run_dir)
    assert res.results["checkpoint_stages"] == list(CHECKPOINT_STAGES)
    names = sorted(p.name for p in run_dir.iterdir())
    assert names == sorted(
        [
            "manifest.json",
            "progress.jsonl",
            *(f"01_floorplan.{e}" for e in ("odb", "def", "sdc")),
            *(f"02_place.{e}" for e in ("odb", "def", "sdc")),
            *(f"03_cts.{e}" for e in ("odb", "def", "sdc")),
            *(
                f"04_global_route.{e}"
                for e in ("odb", "def", "sdc", "guide", "segments")
            ),
        ]
    )
    # Never a name a consumer of the final outputs would take.
    assert not any(".routed." in n for n in names)

    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["run_id"] == run_dir.name
    assert manifest["design"] == DESIGN
    assert manifest["tool"]["version"] == "26Q2-stub"
    script = manifest["inputs"]["script"]
    assert script["sha256"] and script["path"].endswith("pnr.tcl")
    assert manifest["inputs"]["sdc"]["sha256"]
    assert manifest["outcome"]["result"] == "PASS"
    gr = manifest["checkpoints"]["global_route"]
    assert gr["final"] is False and gr["detail_routed"] is False
    assert gr["global_routed"] is True
    assert set(gr["files"]) == {"odb", "def", "sdc", "guides", "segments"}
    assert gr["files"]["segments"]["sha256"]
    place = manifest["checkpoints"]["place"]
    assert place["global_routed"] is False
    assert place["congestion"]["available"] is False

    events = _events(run_dir)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run_start" and kinds[1] == "flow_begin"
    assert kinds[-2:] == ["flow_end", "run_end"]
    # The floorplan checkpoint is written between the macros/PDN and global
    # placement; the CTS one between hold-repair legalization and routing.
    order = [(e["event"], e.get("step") or e.get("stage")) for e in events]
    assert order.index(("checkpoint", "floorplan")) < order.index(
        ("step_begin", "global_placement")
    )
    assert (
        order.index(("step_end", "check_placement"))
        < order.index(("checkpoint", "cts"))
        < order.index(("step_begin", "global_route"))
    )
    assert (
        order.index(("step_end", "global_route"))
        < order.index(("checkpoint", "global_route"))
        < order.index(("step_begin", "detailed_route"))
    )


def test_failure_before_detailed_routing_leaves_labelled_pre_route_checkpoints(
    tmp_path, monkeypatch
):
    """The issue's first acceptance case: the run dies at detailed routing;
    every earlier stage's checkpoint is retained and labelled not final,
    and nothing is published as the routed result."""
    backend, artefacts = _backend(
        tmp_path, monkeypatch, env={"RB_STUB_FAIL": "detailed_route"}
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["checkpoint_stages"] == list(CHECKPOINT_STAGES)
    assert res.results["last_step"]["step"] == "detailed_route"
    assert res.results["last_step"]["status"] == "error"
    for name in _ROUTED:
        assert not (artefacts / name).exists()
    (run_dir,) = _runs(artefacts)
    assert (run_dir / "04_global_route.odb").is_file()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["outcome"]["result"] == "FAIL"
    assert all(c["final"] is False for c in manifest["checkpoints"].values())
    failed = [
        e
        for e in _events(run_dir)
        if e["event"] == "step_end" and e["status"] == "error"
    ]
    assert failed[0]["step"] == "detailed_route"
    assert '"quoted"\nsecond line' in failed[0]["error"]


def test_failure_during_global_route_keeps_the_cts_checkpoint(tmp_path, monkeypatch):
    backend, artefacts = _backend(
        tmp_path, monkeypatch, env={"RB_STUB_FAIL": "global_route"}
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["checkpoint_stages"] == ["floorplan", "place", "cts"]
    (run_dir,) = _runs(artefacts)
    assert (run_dir / "03_cts.odb").is_file()
    assert (run_dir / "03_cts.sdc").is_file()
    # A router that failed is not a routed checkpoint.
    assert not list(run_dir.glob("04_*"))
    for name in _ROUTED:
        assert not (artefacts / name).exists()


def test_a_killed_run_names_the_step_it_was_in(tmp_path, monkeypatch):
    """A wall-limit kill: no Tcl error, no exit — the progress file alone
    says where the run was, and the checkpoints before it survive."""
    backend, artefacts = _backend(
        tmp_path, monkeypatch, env={"RB_STUB_KILL": "detailed_route"}
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["last_step"] == {"step": "detailed_route", "status": "running"}
    (run_dir,) = _runs(artefacts)
    events = _events(run_dir)
    assert events[-2]["event"] == "step_begin"
    assert events[-2]["step"] == "detailed_route"
    assert "flow_end" not in [e["event"] for e in events]
    assert (run_dir / "04_global_route.segments").is_file()


def test_a_failed_run_does_not_leave_the_previous_routed_outputs(tmp_path, monkeypatch):
    """Checkpoints do not change the routed outputs' lifecycle: a previous
    run's routed database is gone after a checkpointed run fails (#469)."""
    backend, artefacts = _backend(
        tmp_path, monkeypatch, env={"RB_STUB_FAIL": "global_route"}
    )
    for name in _ROUTED:
        (artefacts / name).write_text("previous run\n")

    backend.run()

    for name in _ROUTED:
        assert not (artefacts / name).exists()


def test_a_second_run_never_overwrites_the_first_runs_checkpoints(
    tmp_path, monkeypatch
):
    backend, artefacts = _backend(
        tmp_path, monkeypatch, env={"RB_STUB_FAIL": "detailed_route"}
    )
    backend.run()
    (first,) = _runs(artefacts)
    before = {p.name: p.read_bytes() for p in first.iterdir()}

    again, _ = _backend(tmp_path, monkeypatch, env={"RB_STUB_FAIL": "global_route"})
    again.run()

    runs = _runs(artefacts)
    assert len(runs) == 2
    second = next(r for r in runs if r != first)
    assert {p.name: p.read_bytes() for p in first.iterdir()} == before
    assert os.readlink(artefacts / "checkpoints" / "latest") == second.name
    assert (first / "04_global_route.odb").exists()
    assert not (second / "04_global_route.odb").exists()


def test_a_run_that_never_launches_retires_latest_but_keeps_old_runs(
    tmp_path, monkeypatch
):
    """`latest` answers "which checkpoints are this run's"; a run that died
    before OpenROAD has none, so the pointer goes and the old directory,
    still addressable by its id, stays."""
    from rtl_buddy.tools import pnr_openroad

    backend, artefacts = _backend(tmp_path, monkeypatch)
    backend.run()
    (first,) = _runs(artefacts)

    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _name: None)
    res = backend.run()

    assert "not found" in res.results["desc"]
    assert not os.path.lexists(artefacts / "checkpoints" / "latest")
    assert _runs(artefacts) == [first]


def test_a_run_without_checkpoints_also_retires_latest(tmp_path, monkeypatch):
    backend, artefacts = _backend(tmp_path, monkeypatch)
    backend.run()
    plain, _ = _backend(tmp_path, monkeypatch, checkpoints=None)

    res = plain.run()

    assert res.results["result"] == "PASS"
    assert "checkpoint_dir" not in res.results
    assert not os.path.lexists(artefacts / "checkpoints" / "latest")
    assert len(_runs(artefacts)) == 1


def test_an_empty_stage_list_records_progress_without_databases(tmp_path, monkeypatch):
    backend, artefacts = _backend(tmp_path, monkeypatch, checkpoints=())

    backend.run()

    (run_dir,) = _runs(artefacts)
    assert sorted(p.name for p in run_dir.iterdir()) == [
        "manifest.json",
        "progress.jsonl",
    ]
    assert "step_begin" in [e["event"] for e in _events(run_dir)]


# ---------------------------------------------------------------------------
# Resolving and exporting a checkpoint
# ---------------------------------------------------------------------------


def test_resolve_refuses_a_checkpoint_whose_write_never_completed(tmp_path):
    run_dir = tmp_path / "checkpoints" / "20260925T101500-1"
    run_dir.mkdir(parents=True)
    (run_dir / "03_cts.def").write_text("DESIGN demo_top ;\n")
    (run_dir / "progress.jsonl").write_text(
        json.dumps({"event": "checkpoint_begin", "stage": "cts", "index": "03"})
        + "\n"
        + '{"event": "chec'  # the torn tail of a killed write
    )
    os.symlink(run_dir.name, tmp_path / "checkpoints" / "latest")

    for spec in ("cts", "03_cts", f"{run_dir.name}/cts", str(run_dir / "03_cts.def")):
        why = pnr_checkpoints.resolve_checkpoint(str(tmp_path), spec)
        assert isinstance(why, str) and "was not written" in why, spec


def test_resolve_rejects_unknown_stages_and_a_missing_latest(tmp_path):
    assert "unknown checkpoint" in pnr_checkpoints.resolve_checkpoint(
        str(tmp_path), "detail_route"
    )
    assert "no current checkpointed run" in pnr_checkpoints.resolve_checkpoint(
        str(tmp_path), "cts"
    )
    assert "not a checkpoint run id" in pnr_checkpoints.resolve_checkpoint(
        str(tmp_path), "../../etc/cts"
    )


def _fake_klayout(seen):
    """KLayout stand-in writing wherever the command line says to."""

    def _run(cmd, **_kwargs):
        cmd = list(cmd)
        seen.append(cmd)
        result = MagicMock(stdout="", stderr="", returncode=0)
        if cmd[1:] == ["-v"]:
            result.stdout = "KLayout 0.30.8\n"
            return result
        args = dict(a.split("=", 1) for a in cmd if "=" in a and not a.startswith("-"))
        out = Path(args["out_file"])
        out.write_bytes(b"\x00gds")
        Path(json.loads(Path(args["inputs_json"]).read_text())["report"]).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "design": DESIGN,
                    "out_file": str(out),
                    "complete": True,
                    "missing_cells": [],
                    "allowed_empty_cells": [],
                    "orphan_cells": [],
                    "other_errors": 0,
                    "errors": 0,
                }
            )
        )
        return result

    return _run


def test_export_of_a_pre_route_checkpoint_is_labelled_and_kept_apart(
    tmp_path, monkeypatch
):
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    backend, artefacts = _backend(
        tmp_path, monkeypatch, env={"RB_STUB_FAIL": "global_route"}
    )
    backend.run()
    (run_dir,) = _runs(artefacts)
    # A routed layout from some earlier, successful export: the checkpoint
    # export must neither replace nor remove it.
    (artefacts / "demo_top.gds").write_bytes(b"routed")
    for path in _pdk(tmp_path).get_cell_gds_paths() + [
        _pdk(tmp_path).get_klayout_tech(),
        _pdk(tmp_path).get_tech_lef(),
        _pdk(tmp_path).get_macro_lef(),
    ]:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).touch()

    seen: list = []
    monkeypatch.setattr(pnr_openroad, "_resolve_klayout_exe", lambda: "/opt/klayout")
    monkeypatch.setattr(pnr_openroad.subprocess, "run", _fake_klayout(seen))
    exporter = OpenRoadPnr(
        name="demo/export",
        pnr_cfg=backend.pnr_cfg,
        suite_dir=str(tmp_path),
        root_cfg=backend.root_cfg,
        emit_gds=True,
    )

    res = exporter.export_only(checkpoint="cts")

    assert res.results["result"] == "PASS", res.results
    assert res.results["checkpoint_stage"] == "03_cts"
    assert res.results["checkpoint_run_id"] == run_dir.name
    assert res.results["checkpoint_final"] is False
    assert "not final" in res.results["desc"]
    export_dir = run_dir / "export" / "03_cts"
    assert res.results["gds_path"] == str(export_dir / "demo_top.gds")
    stream = next(c for c in seen if any(a.startswith("in_def=") for a in c))
    assert f"in_def={run_dir / '03_cts.def'}" in stream
    assert (artefacts / "demo_top.gds").read_bytes() == b"routed"

    provenance = json.loads((export_dir / "export.provenance.json").read_text())
    ck = provenance["checkpoint"]
    assert ck["stage"] == "cts" and ck["run_id"] == run_dir.name
    assert ck["final"] is False and ck["global_routed"] is False
    # No global route, so no congestion grid: unavailable, not zero.
    assert ck["congestion"]["available"] is False
    assert ck["congestion"]["reason"]


def test_export_refuses_a_stage_the_run_never_reached(tmp_path, monkeypatch):
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    backend, artefacts = _backend(
        tmp_path, monkeypatch, env={"RB_STUB_FAIL": "global_route"}
    )
    backend.run()
    exporter = OpenRoadPnr(
        name="demo/export",
        pnr_cfg=backend.pnr_cfg,
        suite_dir=str(tmp_path),
        root_cfg=backend.root_cfg,
        emit_gds=True,
    )

    res = exporter.export_only(checkpoint="global_route")

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "setup"
    assert "04_global_route was not written" in res.results["desc"]


def test_a_failed_run_logs_where_it_stopped_and_what_it_kept(tmp_path, monkeypatch):
    import logging

    from rtl_buddy.tools import pnr_openroad

    backend, artefacts = _backend(
        tmp_path, monkeypatch, env={"RB_STUB_FAIL": "global_route"}
    )
    events = []
    real = pnr_openroad.log_event

    def _record(logger, level, event, /, **fields):
        events.append((level, event, fields))
        return real(logger, level, event, **fields)

    monkeypatch.setattr(pnr_openroad, "log_event", _record)

    backend.run()

    (level, fields) = next(
        (lv, f) for lv, e, f in events if e == "pnr.checkpoints_retained"
    )
    assert level == logging.WARNING
    assert fields["step"] == "global_route" and fields["step_status"] == "error"
    assert fields["stages"] == ["floorplan", "place", "cts"]
    (run_dir,) = _runs(artefacts)
    assert fields["dir"] == str(run_dir)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("error", "failed in step global_route (error)"),
        ("running", "failed in step global_route (running)"),
        # The last traced step finished; an untraced command after it (a
        # blockage, a user snippet) stopped the run.
        ("ok", "failed after step global_route"),
    ],
)
def test_retained_checkpoint_message_names_where_the_run_stopped(status, expected):
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(
        "pnr.checkpoints_retained",
        {
            "pnr": "p",
            "step": "global_route",
            "step_status": status,
            "stages": ["cts"],
            "dir": "d",
        },
    )
    assert expected in msg
    assert "(ok)" not in msg


def test_a_filesystem_without_symlinks_keeps_the_run(tmp_path, monkeypatch):
    """A failed `latest` pointer is a warning, not a failed setup (#653)."""
    from rtl_buddy.tools import pnr_checkpoints

    def _no_symlinks(*_a, **_k):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(pnr_checkpoints.os, "symlink", _no_symlinks)
    run_dir = pnr_checkpoints.allocate_run_dir(str(tmp_path))
    manifest = pnr_checkpoints.begin_run(
        run_dir,
        artefact_dir=str(tmp_path),
        run="demo",
        design="top",
        stages=("cts",),
        inputs={},
        openroad={"path": "openroad", "version": None},
    )
    assert os.path.isfile(manifest)
    assert not os.path.lexists(pnr_checkpoints.latest_pointer(str(tmp_path)))


def test_progress_with_a_non_utf8_byte_still_reads(tmp_path):
    from rtl_buddy.tools import pnr_checkpoints

    (tmp_path / pnr_checkpoints.PROGRESS_NAME).write_bytes(
        b'{"event": "step_begin", "step": "a"}\n'
        b'{"event": "step_end", "step": "a", "error": "caf\xe9"}\n'
    )
    events = pnr_checkpoints.read_progress(str(tmp_path))
    assert [e["event"] for e in events] == ["step_begin", "step_end"]
