"""Tests for staged P&R runs, `--stop-after`, `--resume`, and per-stage resources.

Staged sessions run the generated stage scripts under the stub OpenROAD harness of
`test_pnr_checkpoints`; the harness's writers and readers make real files, so a
resumed run reads what an earlier run wrote.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_pnr_checkpoints import (
    STUB_RESOURCES,
    _backend,
    _events,
    _pdk,
    _platform,
    _pnr_cfg,
    _render,
    _runs,
)

from rtl_buddy.config.pnr import CHECKPOINT_STAGES, PNR_STAGES, PnrConfigFile
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_checkpoints, pnr_stages
from rtl_buddy.tools.pnr_openroad import STOPPED_MARKER

_NAMES = {
    stage: f"{pnr_checkpoints.STAGE_INDEX[stage]}_{stage}" for stage in PNR_STAGES
}


def _staged(tmp_path, monkeypatch, **kw):
    return _backend(tmp_path, monkeypatch, stages=True, **kw)


def _only_run(artefacts: Path) -> Path:
    (run_dir,) = _runs(artefacts)
    return run_dir


def test_a_staged_run_runs_each_stage_in_its_own_session(tmp_path, monkeypatch):
    backend, artefacts = _staged(tmp_path, monkeypatch)

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["routed"] is True
    run_dir = _only_run(artefacts)
    for stage in PNR_STAGES:
        assert (run_dir / f"{_NAMES[stage]}.tcl").is_file()
        assert (run_dir / f"{_NAMES[stage]}.log").is_file()
    for stage in CHECKPOINT_STAGES:
        assert (run_dir / f"{_NAMES[stage]}.odb").is_file()
        assert (run_dir / f"{_NAMES[stage]}.state.tcl").is_file()
    # pnr.log is the chain's stage logs in order.
    assert (artefacts / "pnr.log").read_bytes() == b"".join(
        (run_dir / f"{_NAMES[stage]}.log").read_bytes() for stage in PNR_STAGES
    )
    events = _events(run_dir)
    ended = [e for e in events if e["event"] == "stage_end"]
    assert [e["stage"] for e in ended] == list(PNR_STAGES)
    assert all(e["measured_by"] == "process" for e in ended)
    assert all(e["wall_s"] == STUB_RESOURCES["wall_s"] for e in ended)
    assert all("checkpoint" in e for e in ended[:-1])
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["staged"] is True
    assert [c["stage"] for c in manifest["chain"]] == list(PNR_STAGES)
    assert all(c["script"]["identity"] for c in manifest["chain"])
    assert list(manifest["resources"]) == list(PNR_STAGES)
    assert manifest["resources"]["cts"]["cpu_s"] == STUB_RESOURCES["cpu_s"]
    assert list(manifest["checkpoints"]) == list(CHECKPOINT_STAGES)


def test_each_stage_session_reads_its_predecessor_checkpoint(tmp_path, monkeypatch):
    backend, artefacts = _staged(tmp_path, monkeypatch)
    backend.run()
    run_dir = _only_run(artefacts)

    floorplan = (run_dir / "01_floorplan.tcl").read_text()
    assert "read_verilog $NETLIST" in floorplan and "read_db" not in floorplan
    for stage in PNR_STAGES[1:]:
        script = (run_dir / f"{_NAMES[stage]}.tcl").read_text()
        pred = _NAMES[pnr_stages.predecessor(stage)]
        assert f"read_db [file join $RB_PRED_DIR {pred}.odb]" in script
        assert f"read_sdc [file join $RB_PRED_DIR {pred}.sdc]" in script
        assert f"source [file join $RB_PRED_DIR {pred}.state.tcl]" in script
        assert "read_verilog" not in script
        # Liberty and LEF are read before the database.
        assert script.index("read_lef") < script.index("read_db")
        assert script.index("read_db") < script.index("set block [ord::get_db_block]")
        assert f"set RB_PRED_DIR {_quoted(run_dir)}" in script
    for stage in ("global_route", "detailed_route", "finish"):
        script = (run_dir / f"{_NAMES[stage]}.tcl").read_text()
        assert script.count("set_propagated_clock [all_clocks]") == 1
    for stage in ("floorplan", "place"):
        script = (run_dir / f"{_NAMES[stage]}.tcl").read_text()
        assert "set_propagated_clock" not in script
    for stage in ("detailed_route", "finish"):
        script = (run_dir / f"{_NAMES[stage]}.tcl").read_text()
        assert script.count("set_routing_layers") == 1
    detailed = (run_dir / "05_detailed_route.tcl").read_text()
    assert (
        "read_global_route_segments [file join $RB_PRED_DIR 04_global_route.segments]"
        in detailed
    )
    # Every session takes its guides from its database; a routed one its global route too.
    finish = (run_dir / "07_finish.tcl").read_text()
    for script in (detailed, finish):
        assert "read_guides" not in script
    assert "read_global_route_segments" not in finish
    assert "sta::max_fanout_min_slack" in finish
    assert "sta::max_fanout_min_slack" not in detailed
    assert not (run_dir / "05_detailed_route.segments").exists()
    for stage in PNR_STAGES[:-1]:
        script = (run_dir / f"{_NAMES[stage]}.tcl").read_text()
        assert script.rstrip().endswith("exit 0")
        assert f"rb::ckpt::stage_end {stage}\n" in script


def _quoted(path) -> str:
    return '"' + str(path) + '"'


def test_stage_scripts_are_the_one_session_flow_split_at_its_markers(tmp_path):
    """Each stage's flow text appears once, in order, in the stage scripts, so a
    staged run runs the same commands as an unbroken one.
    """
    from importlib.resources import files

    template = files("rtl_buddy.pnr").joinpath("flow.tcl.template").read_text()
    flow = pnr_stages.FlowTemplate(template)
    assert (
        flow.head()
        + flow.piece("load")
        + flow.piece("session")
        + "".join(flow.body(stage) for stage in PNR_STAGES)
        == flow.whole()
    )
    assert flow.whole() == re.sub(r"\{\{ @/?\w+ \}\}", "", template)


def test_unbroken_render_is_unchanged_but_for_the_checkpoint_block(tmp_path):
    """With `checkpoints:` the flow differs from a plain render only by the
    checkpoint block and the global-route stage end after hold repair.
    """
    (tmp_path / "plain").mkdir()
    (tmp_path / "armed").mkdir()
    plain = _render(tmp_path / "plain", None).read_text()
    armed = _render(tmp_path / "armed", CHECKPOINT_STAGES).read_text()
    block_start = armed.index('\nputs ">>> Stage checkpoints (#653)"')
    block_end = armed.index("\n", armed.index("\nrb::ckpt::arm ") + 1) + 1
    without = armed[:block_start] + armed[block_end:]
    without = without.replace("rb::ckpt::stage_end global_route\n", "", 1)
    assert without.replace(str(tmp_path / "armed"), "X") == plain.replace(
        str(tmp_path / "plain"), "X"
    )
    assert "{{" not in plain


def _route_hooks(tmp_path) -> dict:
    hooks = {}
    for point in ("after-global-route", "before-detailed-route"):
        path = tmp_path / "hooks" / f"{point}.tcl"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f'puts "MARK {point}"\n')
        hooks[point] = (str(path),)
    return hooks


_HOLD_REPAIR = 'puts ">>> Post-global-route hold repair"'


def test_global_route_checkpoint_follows_hold_repair_and_precedes_hooks(tmp_path):
    cfg = replace(_pnr_cfg(tmp_path, CHECKPOINT_STAGES), hooks=_route_hooks(tmp_path))
    text = _render(
        tmp_path, CHECKPOINT_STAGES, pnr_cfg=cfg, global_route_hold_repair=True
    ).read_text()
    order = [
        text.index("global_route -congestion_iterations"),
        text.index("rb::hook::run after-global-route"),
        text.index(_HOLD_REPAIR),
        text.index("rb::ckpt::stage_end global_route\n"),
        text.index("rb::hook::run before-detailed-route"),
        text.index('puts ">>> Detail route"'),
    ]
    assert order == sorted(order)


def test_a_staged_global_route_ends_after_its_hooks_and_hold_repair(
    tmp_path, monkeypatch
):
    """The `before-detailed-route` hooks run in the detailed-route session, once its global route is read back."""
    cfg = replace(
        _pnr_cfg(tmp_path, stages=True),
        hooks={"session": _session_hook(tmp_path), **_route_hooks(tmp_path)},
    )
    backend, artefacts = _staged(tmp_path, monkeypatch, pnr_cfg=cfg)
    backend.root_cfg.get_pnr_platform_cfg.return_value = _platform(
        _pdk(tmp_path), global_route_hold_repair=True
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    run_dir = _only_run(artefacts)
    route = (run_dir / "04_global_route.tcl").read_text()
    order = [
        route.index("rb::hook::run after-global-route"),
        route.index(_HOLD_REPAIR),
        route.index("rb::ckpt::stage_end global_route\n"),
    ]
    assert order == sorted(order)
    assert "before-detailed-route" not in route
    detailed = (run_dir / "05_detailed_route.tcl").read_text()
    assert detailed.index("read_global_route_segments") < detailed.index(
        "rb::hook::run before-detailed-route"
    )
    assert (
        "MARK before-detailed-route" in (run_dir / "05_detailed_route.log").read_text()
    )
    # The session hook runs once at the start of every session.
    for stage in PNR_STAGES:
        log = (run_dir / f"{_NAMES[stage]}.log").read_text()
        assert log.count("MARK session") == 1, stage
        script = (run_dir / f"{_NAMES[stage]}.tcl").read_text()
        assert script.count("rb::hook::run session") == 1, stage


def _session_hook(tmp_path) -> tuple[str]:
    path = tmp_path / "hooks" / "session.tcl"
    path.parent.mkdir(exist_ok=True)
    path.write_text('puts "MARK session"\n')
    return (str(path),)


def test_global_placement_only_state_lives_in_the_place_session(tmp_path, monkeypatch):
    """Global-placement-only blockages and rotated-macro footprints exist only around `global_placement`; the cell halos the floorplan recorded reach the place session through its state file."""
    from rtl_buddy.config.pnr import BlockageLifetime, BlockageType, PnrBlockage

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pdn.tcl").write_text("dict set ::MACRO_CELL_HALOS u_x {3 4}\n")
    (hooks / "gp.tcl").write_text('puts "HALOS $::MACRO_CELL_HALOS"\n')
    base = _pnr_cfg(tmp_path, stages=True)
    cfg = replace(
        base,
        floorplan=replace(
            base.floorplan,
            blockages=[
                PnrBlockage(
                    rect=(1.0, 1.0, 5.0, 5.0),
                    type=BlockageType.SOFT,
                    lifetime=BlockageLifetime.GLOBAL_PLACEMENT,
                )
            ],
        ),
        hooks={
            "after-pdn": (str(hooks / "pdn.tcl"),),
            "after-global-placement": (str(hooks / "gp.tcl"),),
        },
    )
    backend, artefacts = _staged(tmp_path, monkeypatch, pnr_cfg=cfg)
    monkeypatch.setattr(backend, "_has_tcl_command", lambda *_a: True)

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    run_dir = _only_run(artefacts)
    scripts = {s: (run_dir / f"{_NAMES[s]}.tcl").read_text() for s in PNR_STAGES}
    for text in (
        ">>> Global-placement-only blockages",
        "rb::rotated::block_footprints",
        "odb::dbBlockage_destroy",
    ):
        assert [s for s in PNR_STAGES if text in scripts[s]] == ["place"], text
    assert "MACRO_CELL_HALOS" in (run_dir / "01_floorplan.state.tcl").read_text()
    assert "HALOS u_x {3 4}" in (run_dir / "02_place.log").read_text()


def test_stop_after_in_a_staged_run_ends_after_that_stage(tmp_path, monkeypatch):
    backend, artefacts = _staged(tmp_path, monkeypatch, stop_after="global_route")

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["routed"] is False
    assert res.results["stopped_after"] == "global_route"
    assert "not routed" in res.results["desc"]
    run_dir = _only_run(artefacts)
    assert (run_dir / "04_global_route.odb").is_file()
    assert not (run_dir / "05_detailed_route.tcl").exists()
    log = (artefacts / "pnr.log").read_text()
    assert f"{STOPPED_MARKER}global_route" in log.splitlines()
    assert not list(artefacts.glob("*.routed.*"))
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["stop_after"] == "global_route"
    assert list(manifest["resources"]) == list(PNR_STAGES[:4])


def test_stop_after_in_an_unbroken_run_records_resources_per_stage(
    tmp_path, monkeypatch
):
    backend, artefacts = _backend(
        tmp_path, monkeypatch, checkpoints=None, stop_after="detailed_route"
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["routed"] is False
    assert res.results["stopped_after"] == "detailed_route"
    run_dir = _only_run(artefacts)
    assert res.results["checkpoint_stages"] == ["detailed_route"]
    assert (run_dir / "05_detailed_route.odb").is_file()
    assert not list(artefacts.glob("*.routed.*"))
    ended = [e for e in _events(run_dir) if e["event"] == "stage_end"]
    # Measured inside the session; the stop ends detailed routing, and no finish
    # stage starts.
    assert [e["stage"] for e in ended] == list(CHECKPOINT_STAGES)
    assert all(e["measured_by"] == "session" for e in ended)
    assert all(e["wall_s"] >= 0 for e in ended)
    if sys.platform.startswith("linux"):
        assert all(isinstance(e["cpu_s"], float) for e in ended)
        assert all(isinstance(e["peak_rss_kb"], int) for e in ended[:1])


def test_a_finished_unbroken_run_with_checkpoints_records_every_stage(
    tmp_path, monkeypatch
):
    backend, artefacts = _backend(tmp_path, monkeypatch)

    res = backend.run()

    assert res.results["routed"] is True
    manifest = json.loads((_only_run(artefacts) / "manifest.json").read_text())
    assert list(manifest["resources"]) == list(PNR_STAGES)
    assert manifest["resources"]["finish"]["status"] == "ok"


def test_a_stopped_run_without_its_marker_fails(tmp_path, monkeypatch):
    """A stopped run is told from an accidental early exit by its marker."""
    backend, artefacts = _backend(
        tmp_path,
        monkeypatch,
        checkpoints=None,
        stop_after="cts",
        env={"RB_STUB_EXIT": "check_placement"},
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "incomplete"
    assert "--stop-after cts" in res.results["desc"]


def test_a_staged_session_that_exits_early_fails_the_chain(tmp_path, monkeypatch):
    backend, artefacts = _staged(
        tmp_path, monkeypatch, env={"RB_STUB_EXIT": "clock_tree_synthesis"}
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "incomplete"
    assert "03_cts" in res.results["desc"]
    run_dir = _only_run(artefacts)
    assert not (run_dir / "04_global_route.log").exists()


def test_a_failed_staged_session_names_its_stage(tmp_path, monkeypatch):
    backend, artefacts = _staged(
        tmp_path, monkeypatch, env={"RB_STUB_FAIL": "global_route"}
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["desc"].startswith("stage 04_global_route: OpenROAD exited")
    assert "fail_stage" not in res.results
    run_dir = _only_run(artefacts)
    ended = {e["stage"]: e for e in _events(run_dir) if e["event"] == "stage_end"}
    assert ended["global_route"]["status"] == "error"
    assert "checkpoint" not in ended["global_route"]


def test_a_staged_checkpoint_that_cannot_be_written_fails_as_checkpoint(
    tmp_path, monkeypatch
):
    backend, artefacts = _staged(
        tmp_path, monkeypatch, env={"RB_STUB_FAIL": "write_db"}
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "checkpoint"
    assert res.results["desc"].startswith("stage 01_floorplan: ")
    assert not (_only_run(artefacts) / "02_place.log").exists()


def test_a_stop_checkpoint_that_cannot_be_written_fails_as_checkpoint(
    tmp_path, monkeypatch
):
    backend, _ = _backend(
        tmp_path,
        monkeypatch,
        checkpoints=None,
        stop_after="place",
        env={"RB_STUB_FAIL": "write_db"},
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "checkpoint"


def test_an_error_line_in_a_staged_session_stops_the_chain(tmp_path, monkeypatch):
    """A session that logs an OpenROAD `[ERROR` but exits 0 fails the run after its
    stage, which is recorded not clean, so no resume can start past it.
    """
    backend, artefacts = _staged(
        tmp_path, monkeypatch, env={"RB_STUB_ERRLINE": "check_placement"}
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["desc"] == "stage 03_cts: 1 ERROR(s) in OpenROAD log"
    assert "fail_stage" not in res.results or res.results["fail_stage"] is None
    run_dir = _only_run(artefacts)
    assert not (run_dir / "04_global_route.log").exists()
    ended = {e["stage"]: e for e in _events(run_dir) if e["event"] == "stage_end"}
    assert ended["cts"]["status"] == "error"
    assert "checkpoint" not in ended["cts"]

    desc = _refusal(tmp_path, monkeypatch, run_dir.name, from_stage="global_route")
    assert "stage cts" in desc and "did not finish cleanly" in desc


def test_a_hook_that_wraps_an_anchor_fails_the_unbroken_run_as_checkpoint(
    tmp_path, monkeypatch
):
    hook = tmp_path / "wrap_cts.tcl"
    hook.write_text(
        "rename clock_tree_synthesis rb_test_cts\n"
        "proc clock_tree_synthesis {args} { rb_test_cts {*}$args }\n"
    )
    cfg = replace(
        _pnr_cfg(tmp_path, CHECKPOINT_STAGES),
        hooks={"after-repair-design": (str(hook),)},
    )
    backend, _ = _backend(tmp_path, monkeypatch, pnr_cfg=cfg)

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "checkpoint"
    assert "checkpoint 02_place was not written" in res.results["desc"]
    assert "entering clock_tree_synthesis" in res.results["desc"]


def _xfail_driver(monkeypatch, result: str):
    """`_stage_driver`, with each run returning `result` (PASS or FAIL)."""
    from test_pnr_plan import _Locks

    from rtl_buddy.rtl_buddy import RtlBuddy
    from rtl_buddy.runner.pnr_results import PnrFailResults, PnrPassResults

    class _Runner:
        def __init__(self, *, name, **kw):
            self.name = name

        def run(self):
            if result == "PASS":
                return PnrPassResults(
                    name=f"{self.name}/results",
                    fields={"routed": False, "stopped_after": "cts"},
                )
            return PnrFailResults(name=f"{self.name}/results", desc="1 ERROR(s)")

    monkeypatch.setattr("rtl_buddy.rtl_buddy.PnrRunner", _Runner)
    rb = RtlBuddy.__new__(RtlBuddy)
    rb.root_cfg = None
    rb._artifact_locks = _Locks()
    return rb


@pytest.mark.parametrize("marker", ["xfail", "xfail_strict"])
@pytest.mark.parametrize("result", ["PASS", "FAIL"])
def test_xfail_does_not_apply_to_a_stopped_run(tmp_path, monkeypatch, result, marker):
    from test_pnr_plan import _run, _suite

    run = _run("a", xfail=True).replace("xfail: true", f"{marker}: true")
    suite = _suite(tmp_path / "pnr.yaml", run)
    rb = _xfail_driver(monkeypatch, result)

    (row,) = rb._do_pnr_suite(suite, pnr_name="a", stop_after="cts")

    assert row["results"].results["result"] == result


def test_xfail_applies_to_a_full_run(tmp_path, monkeypatch):
    from test_pnr_plan import _run, _suite

    suite = _suite(tmp_path / "pnr.yaml", _run("a", xfail=True))
    rb = _xfail_driver(monkeypatch, "PASS")

    (row,) = rb._do_pnr_suite(suite, pnr_name="a")

    assert row["results"].results["result"] == "XPASS"


@pytest.mark.parametrize(
    ("env", "excused"),
    [
        ({"RB_STUB_ERRLINE": "global_route"}, True),
        ({"RB_STUB_EXIT": "global_route"}, False),
        ({"RB_STUB_FAIL": "write_db"}, False),
    ],
)
def test_xfail_excuses_a_staged_verdict_but_not_a_broken_chain(
    tmp_path, monkeypatch, env, excused
):
    from rtl_buddy.runner.xfail import apply_xfail

    backend, _ = _staged(tmp_path, monkeypatch, env=env)
    res = backend.run()
    assert res.results["result"] == "FAIL"

    apply_xfail(res)

    assert (res.results["result"] == "XFAIL") is excused, res.results["desc"]


def test_a_staged_run_hardens_in_its_finish_session(tmp_path, monkeypatch):
    from rtl_buddy.tools import pnr_openroad

    cfg = replace(_pnr_cfg(tmp_path, stages=True), harden=True)
    backend, artefacts = _staged(tmp_path, monkeypatch, pnr_cfg=cfg)
    openroad = pnr_openroad.subprocess.run

    def _run(cmd, **kwargs):
        if os.path.basename(str(cmd[0])) != "klayout":
            return openroad(cmd, **kwargs)
        (artefacts / "demo_top.gds").write_bytes(b"\x00\x06\x00\x02\x00\x07block")
        (artefacts / "def2stream.report.json").write_text(
            json.dumps({"schema": 1, "design": "demo_top", "complete": True})
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)
    monkeypatch.setattr(pnr_openroad, "_resolve_klayout_exe", lambda: "/opt/klayout")
    for path in ("lef/tech.lef", "lef/cells.lef", "klayout/tech.lyt", "gds/cells.gds"):
        (tmp_path / "pdk" / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "pdk" / path).write_text("x\n")

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["abstract_dir"] == str(artefacts / "abstract")
    assert (artefacts / "abstract" / "demo_top.lef").is_file()
    run_dir = _only_run(artefacts)
    hardened = [
        s
        for s in PNR_STAGES
        if "write_abstract_lef" in (run_dir / f"{_NAMES[s]}.tcl").read_text()
    ]
    assert hardened == ["finish"]


def _hook(tmp_path, point, text) -> tuple[str]:
    path = tmp_path / "hooks" / f"{point}.tcl"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    return (str(path),)


def test_a_staged_run_warns_about_commands_a_hook_leaves_in_its_session(
    tmp_path, monkeypatch, caplog
):
    """A hook other than `session` that defines or renames a global command warns; later stage sessions do not have it."""
    hooks = {
        "session": _hook(tmp_path, "session", "proc rb_test_session {} {}\n"),
        "after-cts": _hook(
            tmp_path,
            "after-cts",
            "proc rb_test_helper {} {}\nrename check_placement rb_test_check\n",
        ),
        "after-global-route": _hook(tmp_path, "after-global-route", "set x 1\n"),
        "before-reports": _hook(
            tmp_path, "before-reports", "proc rb_test_report {} {}\n"
        ),
    }
    cfg = replace(_pnr_cfg(tmp_path, stages=True), hooks=hooks)
    backend, artefacts = _staged(tmp_path, monkeypatch, pnr_cfg=cfg)

    with caplog.at_level("WARNING"):
        res = backend.run()

    assert res.results["result"] == "PASS", res.results
    warned = [
        r.rtl_fields
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "pnr.hook_commands_not_kept"
    ]
    assert warned == [
        {
            "pnr": "demo_pnr",
            "point": "after-cts",
            "path": hooks["after-cts"][0],
            "commands": ["check_placement", "rb_test_check", "rb_test_helper"],
        }
    ]
    message = next(
        r.getMessage()
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "pnr.hook_commands_not_kept"
    )
    assert "later stage sessions do not have" in message
    assert "session hook" in message


def test_a_failing_hook_still_fails_a_staged_session(tmp_path, monkeypatch):
    hooks = {"after-cts": _hook(tmp_path, "after-cts", "error boom\n")}
    cfg = replace(_pnr_cfg(tmp_path, stages=True), hooks=hooks)
    backend, artefacts = _staged(tmp_path, monkeypatch, pnr_cfg=cfg)

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["desc"].startswith("stage 03_cts: ")
    log = (_only_run(artefacts) / "03_cts.log").read_text()
    assert "Hook after-cts" in log and "boom" in log


def _stopped_at_global_route(tmp_path, monkeypatch) -> Path:
    backend, artefacts = _staged(tmp_path, monkeypatch, stop_after="global_route")
    assert backend.run().results["result"] == "PASS"
    return _only_run(artefacts)


def test_resume_twice_from_one_global_route(tmp_path, monkeypatch):
    first = _stopped_at_global_route(tmp_path, monkeypatch)

    results = []
    for _ in range(2):
        backend, artefacts = _backend(
            tmp_path,
            monkeypatch,
            resume=first.name,
            resume_from="detailed_route",
        )
        results.append(backend.run())

    for res in results:
        assert res.results["result"] == "PASS", res.results
        assert res.results["routed"] is True
    runs = [r for r in _runs(artefacts) if r != first]
    assert len(runs) == 2
    for run_dir in runs:
        script = (run_dir / "05_detailed_route.tcl").read_text()
        assert f"set RB_PRED_DIR {_quoted(first)}" in script
        assert not (run_dir / "04_global_route.tcl").exists()
        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert manifest["resumed_from"]["run_id"] == first.name
        assert manifest["resumed_from"]["from"] == "detailed_route"
        assert manifest["resumed_from"]["checkpoint"] == "04_global_route"
        assert [c["stage"] for c in manifest["chain"]] == list(PNR_STAGES)
        assert manifest["chain"][3]["inherited"] is True
        assert manifest["checkpoints"]["global_route"]["inherited_from"] == first.name
        assert "inherited_from" not in manifest["checkpoints"]["detailed_route"]
        assert list(manifest["resources"]) == ["detailed_route", "finish"]
        # An inherited checkpoint is exported from the run that wrote it.
        why = pnr_checkpoints.resolve_checkpoint(
            str(artefacts), f"{run_dir.name}/global_route"
        )
        assert f"name {first.name}/global_route" in why
        # pnr.log carries the inherited stages' logs first.
        log = (artefacts / "pnr.log").read_bytes()
        assert log.startswith((first / "01_floorplan.log").read_bytes())
    # The original checkpoint is untouched.
    assert (first / "04_global_route.odb").is_file()


def test_a_resume_from_finish_reports_the_inherited_route_drcs(tmp_path, monkeypatch):
    backend, artefacts = _staged(
        tmp_path, monkeypatch, stop_after="detailed_route", env={"RB_STUB_DRC": "3"}
    )
    stopped = backend.run()
    assert stopped.results["drc_count"] == 3, stopped.results
    first = _only_run(artefacts)
    drc = (artefacts / "route.drc.rpt").read_bytes()
    assert (first / "05_detailed_route.route.drc.rpt").read_bytes() == drc
    assert (first / "05_detailed_route.route.maze.log").is_file()
    (artefacts / "route.maze.log").write_text("another run's maze log\n")

    backend, _ = _backend(
        tmp_path, monkeypatch, resume=first.name, resume_from="finish"
    )
    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["drc_count"] == 3
    assert (artefacts / "route.drc.rpt").read_bytes() == drc
    assert (artefacts / "route.maze.log").read_bytes() == (
        first / "05_detailed_route.route.maze.log"
    ).read_bytes()
    second = next(r for r in _runs(artefacts) if r != first)
    manifest = json.loads((second / "manifest.json").read_text())
    kept = manifest["checkpoints"]["detailed_route"]
    assert kept["inherited_from"] == first.name
    assert Path(kept["files"]["drc"]["path"]).name == "05_detailed_route.route.drc.rpt"


def test_a_resume_may_change_settings_of_the_stages_it_runs(tmp_path, monkeypatch):
    first = _stopped_at_global_route(tmp_path, monkeypatch)
    cfg = replace(_pnr_cfg(tmp_path, stages=True), detailed_route_verbose=2)

    backend, _ = _backend(
        tmp_path,
        monkeypatch,
        pnr_cfg=cfg,
        resume=str(first),
        resume_from="detailed_route",
    )

    assert backend.run().results["result"] == "PASS"


def _snapshot(root: Path) -> dict:
    """Every path under `root` with its link target or content."""
    if not root.is_dir():
        return {}
    return {
        str(p.relative_to(root)): (
            os.readlink(p)
            if p.is_symlink()
            else p.read_bytes()
            if p.is_file()
            else None
        )
        for p in root.rglob("*")
    }


def test_a_resume_may_run_at_another_thread_count(tmp_path, monkeypatch):
    """`threads: auto` on a bigger allocation resumes, and each stage records its own count."""
    from rtl_buddy.config import openroad_threads

    cfg = replace(_pnr_cfg(tmp_path, stages=True), threads="auto")
    for cpus in (2, 4):
        monkeypatch.setattr(
            openroad_threads,
            "detect_allocation",
            lambda *a, cpus=cpus, **k: (cpus, "SLURM_CPUS_PER_TASK"),
        )
        options = (
            {"stop_after": "global_route"}
            if cpus == 2
            else {"resume": "latest", "resume_from": "detailed_route"}
        )
        backend, artefacts = _backend(tmp_path, monkeypatch, pnr_cfg=cfg, **options)
        res = backend.run()
        assert res.results["result"] == "PASS", res.results

    first, second = _runs(artefacts)
    for run_dir, stages, threads in (
        (first, PNR_STAGES[:4], 2),
        (second, PNR_STAGES[4:], 4),
    ):
        ended = [e for e in _events(run_dir) if e["event"] == "stage_end"]
        assert [(e["stage"], e["threads"]) for e in ended] == [
            (stage, threads) for stage in stages
        ]
        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert {r["threads"] for r in manifest["resources"].values()} == {threads}
    assert "set_thread_count 2" in (first / "01_floorplan.tcl").read_text()
    assert "set_thread_count 4" in (second / "05_detailed_route.tcl").read_text()


def _with_platform(backend, tmp_path, signal="metal2-metal6", **pdk_changes):
    """Point `backend` at the stub platform with its signal layers or PDK fields changed."""
    from test_pnr_checkpoints import _pdk, _platform

    platform = _platform(_pdk(tmp_path, **pdk_changes), signal=signal)
    backend.root_cfg.get_pnr_platform_cfg.return_value = platform
    return backend


def test_a_routing_layer_change_resumes_only_where_no_inherited_stage_reads_it(
    tmp_path, monkeypatch
):
    first = _stopped_at_global_route(tmp_path, monkeypatch)

    backend, _ = _backend(
        tmp_path, monkeypatch, resume=first.name, resume_from="global_route"
    )
    res = _with_platform(backend, tmp_path, signal="metal2-metal5").run()
    assert res.results["result"] == "PASS", res.results

    backend, artefacts = _backend(
        tmp_path, monkeypatch, resume=first.name, resume_from="detailed_route"
    )
    _with_platform(backend, tmp_path, signal="metal2-metal5")
    before = _snapshot(artefacts)
    res = backend.run()
    assert res.results["fail_stage"] == "resume"
    assert "04_global_route.tcl" in res.results["desc"]
    assert "set SIGNAL_LAYERS   metal2-metal5" in res.results["desc"]
    assert _snapshot(artefacts) == before


def test_a_fill_cell_change_resumes_from_finish(tmp_path, monkeypatch):
    backend, artefacts = _staged(tmp_path, monkeypatch, stop_after="detailed_route")
    assert backend.run().results["result"] == "PASS"
    first = _only_run(artefacts)

    backend, _ = _backend(
        tmp_path, monkeypatch, resume=first.name, resume_from="finish"
    )
    _with_platform(backend, tmp_path, fill_cells=["FILLCELL_X1", "FILLCELL_X2"])
    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    second = next(r for r in _runs(artefacts) if r != first)
    assert "FILLCELL_X2" in (second / "07_finish.tcl").read_text()


def _two_corners(backend, tmp_path):
    """Point `backend` at a `tt` + `ss` platform whose Liberty files exist."""
    from test_pnr_checkpoints import _pdk, _platform

    corners = {}
    for corner in ("tt", "ss"):
        lib = tmp_path / "pdk" / "lib" / f"{corner}.lib"
        lib.parent.mkdir(parents=True, exist_ok=True)
        if not lib.exists():
            lib.write_text(f'library ({corner}) {{ time_unit : "1ns" ; }}\n')
        corners[corner] = f"pdk/lib/{corner}.lib"
    backend.root_cfg.get_pnr_platform_cfg.return_value = _platform(
        _pdk(tmp_path, corners=corners), sta_corners=["tt", "ss"]
    )
    return backend


def test_every_session_of_a_multi_corner_run_reads_every_corner(tmp_path, monkeypatch):
    backend, artefacts = _staged(tmp_path, monkeypatch, stop_after="cts")

    res = _two_corners(backend, tmp_path).run()

    assert res.results["result"] == "PASS", res.results
    run_dir = _only_run(artefacts)
    ss = tmp_path / "pdk" / "lib" / "ss.lib"
    for stage in PNR_STAGES[:3]:
        script = (run_dir / f"{_NAMES[stage]}.tcl").read_text()
        assert "define_corners tt ss" in script
        assert f"read_liberty -corner ss {ss}" in script
        assert script.index(f"read_liberty -corner ss {ss}") < script.index("read_lef")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert [Path(f["path"]).name for f in manifest["inputs"]["liberty"]] == [
        "tt.lib",
        "ss.lib",
    ]


def test_resume_refuses_a_changed_liberty_of_any_corner(tmp_path, monkeypatch):
    backend, artefacts = _staged(tmp_path, monkeypatch, stop_after="cts")
    assert _two_corners(backend, tmp_path).run().results["result"] == "PASS"
    first = _only_run(artefacts)
    ss = tmp_path / "pdk" / "lib" / "ss.lib"
    ss.write_text(ss.read_text() + "/* recharacterised */\n")

    backend, _ = _backend(tmp_path, monkeypatch, resume=first.name, resume_from="cts")
    _two_corners(backend, tmp_path)
    before = _snapshot(artefacts)
    res = backend.run()

    assert res.results["fail_stage"] == "resume"
    assert "input liberty differs" in res.results["desc"]
    assert "ss.lib" in res.results["desc"]
    assert _snapshot(artefacts) == before


def _refusal(tmp_path, monkeypatch, run, from_stage="detailed_route", **kw) -> str:
    backend, artefacts = _backend(
        tmp_path, monkeypatch, resume=run, resume_from=from_stage, **kw
    )
    before = _snapshot(artefacts)
    res = backend.run()
    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "resume"
    assert res.results["desc"].startswith("resume refused: ")
    # A refused resume clears and writes nothing: no run directory, and `latest` and
    # the previous outputs stay as they were.
    assert _snapshot(artefacts) == before
    return res.results["desc"]


def test_resume_refuses_a_changed_setting_of_an_earlier_stage(tmp_path, monkeypatch):
    first = _stopped_at_global_route(tmp_path, monkeypatch)
    cfg = _pnr_cfg(tmp_path, stages=True)
    cfg = replace(cfg, floorplan=replace(cfg.floorplan, utilization=0.6))

    desc = _refusal(tmp_path, monkeypatch, first.name, pnr_cfg=cfg)

    assert "01_floorplan.tcl" in desc
    assert "set CORE_UTIL_PCT   55.00" in desc and "60.00" in desc


def test_a_refused_resume_keeps_latest_for_the_next_try(tmp_path, monkeypatch):
    first = _stopped_at_global_route(tmp_path, monkeypatch)
    cfg = _pnr_cfg(tmp_path, stages=True)
    _refusal(
        tmp_path,
        monkeypatch,
        "latest",
        pnr_cfg=replace(cfg, floorplan=replace(cfg.floorplan, utilization=0.6)),
    )

    backend, _ = _backend(
        tmp_path, monkeypatch, resume="latest", resume_from="detailed_route"
    )
    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["resumed_from"]["run_id"] == first.name


def test_resume_refuses_a_changed_input_file(tmp_path, monkeypatch):
    first = _stopped_at_global_route(tmp_path, monkeypatch)
    sdc = tmp_path / "constraints.sdc"
    original = sdc.read_text()

    def _pnr_cfg_with_new_sdc(*a, **k):
        cfg = _pnr_cfg(*a, **k)
        sdc.write_text(original + "set_input_delay 0.1 [all_inputs]\n")
        return cfg

    cfg = _pnr_cfg_with_new_sdc(tmp_path, stages=True)

    desc = _refusal(tmp_path, monkeypatch, first.name, pnr_cfg=cfg)

    assert "input sdc differs" in desc


def test_resume_refuses_another_openroad_version(tmp_path, monkeypatch):
    first = _stopped_at_global_route(tmp_path, monkeypatch)
    backend, artefacts = _backend(
        tmp_path, monkeypatch, resume=first.name, resume_from="detailed_route"
    )
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: "26Q4-other")

    res = backend.run()

    assert res.results["fail_stage"] == "resume"
    assert "OpenROAD is 26Q4-other" in res.results["desc"]
    assert "26Q2-stub" in res.results["desc"]


def test_resume_refuses_a_changed_checkpoint(tmp_path, monkeypatch):
    first = _stopped_at_global_route(tmp_path, monkeypatch)
    (first / "04_global_route.odb").write_text("edited\n")

    desc = _refusal(tmp_path, monkeypatch, first.name)

    assert "04_global_route.odb changed" in desc


def test_resume_refuses_a_stage_the_run_never_finished(tmp_path, monkeypatch):
    first = _stopped_at_global_route(tmp_path, monkeypatch)

    desc = _refusal(tmp_path, monkeypatch, first.name, from_stage="finish")

    assert "stage detailed_route" in desc
    assert "did not finish with its checkpoint written" in desc


def test_resume_refuses_a_failed_stage(tmp_path, monkeypatch):
    backend, artefacts = _staged(
        tmp_path, monkeypatch, env={"RB_STUB_FAIL": "detailed_route"}
    )
    backend.run()
    first = _only_run(artefacts)

    desc = _refusal(tmp_path, monkeypatch, first.name, from_stage="finish")

    assert "stage detailed_route" in desc and "did not finish cleanly" in desc


def test_resume_refuses_an_unbroken_run(tmp_path, monkeypatch):
    backend, artefacts = _backend(tmp_path, monkeypatch)
    backend.run()
    first = _only_run(artefacts)

    desc = _refusal(tmp_path, monkeypatch, first.name)

    assert "was not a staged run" in desc


def test_resume_refuses_an_unknown_run(tmp_path, monkeypatch):
    desc = _refusal(tmp_path, monkeypatch, "20260101T000000-1")

    assert "no checkpoint run '20260101T000000-1'" in desc


def test_resume_latest_names_the_last_run(tmp_path, monkeypatch):
    first = _stopped_at_global_route(tmp_path, monkeypatch)
    backend, artefacts = _backend(
        tmp_path, monkeypatch, resume="latest", resume_from="detailed_route"
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["resumed_from"]["run_id"] == first.name


def test_a_resumed_run_can_itself_be_resumed(tmp_path, monkeypatch):
    first = _stopped_at_global_route(tmp_path, monkeypatch)
    backend, artefacts = _backend(
        tmp_path,
        monkeypatch,
        resume=first.name,
        resume_from="detailed_route",
        stop_after="detailed_route",
    )
    assert backend.run().results["result"] == "PASS"
    second = next(r for r in _runs(artefacts) if r != first)

    backend, _ = _backend(
        tmp_path, monkeypatch, resume=second.name, resume_from="finish"
    )
    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    third = next(r for r in _runs(artefacts) if r not in (first, second))
    manifest = json.loads((third / "manifest.json").read_text())
    assert manifest["checkpoints"]["global_route"]["inherited_from"] == first.name
    assert manifest["checkpoints"]["detailed_route"]["inherited_from"] == second.name
    assert [c["run_id"] for c in manifest["chain"]] == [first.name] * 4 + [
        second.name,
        third.name,
    ]


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"stop_after": "finish"}, "--stop-after finish"),
        ({"resume": "latest"}, "go together"),
        ({"resume_from": "cts"}, "go together"),
        ({"resume": "latest", "resume_from": "floorplan"}, "--from floorplan"),
        (
            {"resume": "latest", "resume_from": "detailed_route", "stop_after": "cts"},
            "comes before",
        ),
    ],
)
def test_stage_options_that_cannot_combine(options, expected):
    merged = {"stop_after": None, "resume": None, "resume_from": None, **options}
    assert expected in pnr_stages.option_error(**merged)


def test_stages_config_writes_every_checkpoint(tmp_path):
    cfg = PnrConfigFile(
        name="r",
        desc="r",
        synth="s",
        synth_path="synth.yaml",
        platform="p",
        stages=True,
    ).initialise(str(tmp_path))
    assert cfg.get_stages() is True
    assert cfg.get_checkpoints() == CHECKPOINT_STAGES


def test_stages_config_refuses_a_partial_checkpoint_list(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="stages: true"):
        PnrConfigFile(
            name="r",
            desc="r",
            synth="s",
            synth_path="synth.yaml",
            platform="p",
            stages=True,
            checkpoints=["cts"],
        ).initialise(str(tmp_path))


def test_script_identity_keeps_only_the_header_lines_the_script_reads():
    header = (
        "# header\nset A 1\nset B 2\n\nset C 3\nset D $C/x\nset E 5\nset F 6\nset G 7\n"
    )
    body = "file mkdir $D\nputs ${B}\nproc p {} { return $::E }\nputs A\nputs $Ffoo\n"

    def ident(text):
        return pnr_stages.script_identity(
            header.replace("set " + text, "set " + text + "0") + body
        )

    base = pnr_stages.script_identity(header + body)
    # Read as $D (and C through D's value), ${B} and $::E: compared.
    for read in ("B 2", "C 3", "D $C/x", "E 5"):
        assert ident(read) != base, read
    # Never read, or only as a bare word or a longer name: not compared.
    for unread in ("A 1", "F 6", "G 7"):
        assert ident(unread) == base, unread
    # rb's procs are compared.
    assert pnr_stages.script_identity(header + body.replace("::E", "::G")) != base


def test_script_identity_ignores_the_thread_count():
    def script(threads):
        block = f'\nputs ">>> Threads"\nset_thread_count {threads}\n' if threads else ""
        return f'file mkdir $OUT_DIR\n{block}\nputs ">>> Stage"\nflow\n'

    assert len({pnr_stages.script_identity(script(n)) for n in (None, 2, 32)}) == 1


def test_script_identity_ignores_run_bindings_and_the_stop_report():
    a = (
        'set RB_RUN_DIR "/a/run1"\nset RB_PRED_DIR "/a/run0"\nflow\n'
        'puts ">>> End of stage 04_global_route"\nexit 0\n'
    )
    b = (
        'set RB_RUN_DIR "/b/run2"\nset RB_PRED_DIR "/b/run1"\nflow\n'
        'puts ">>> End of stage 04_global_route"\nreport_tns\nexit 0\n'
    )
    assert pnr_stages.script_identity(a) == pnr_stages.script_identity(b)
    assert pnr_stages.script_identity(a) != pnr_stages.script_identity(
        a.replace("flow", "other")
    )


def test_run_session_measures_the_child_process(tmp_path):
    from rtl_buddy.tools.pnr_openroad import _run_session

    out = tmp_path / "out.log"
    with open(out, "ab") as log:
        code, stderr, used = _run_session(
            [
                sys.executable,
                "-c",
                "import os, sys; b = bytearray(64 * 1024 * 1024); "
                "print(os.getcwd()); sys.stderr.write('err')",
            ],
            stdout=log,
            env=dict(os.environ),
            cwd=str(tmp_path),
        )
    assert code == 0 and stderr == "err"
    assert out.read_text() == f"{tmp_path}\n"
    assert used["measured_by"] == "process"
    assert used["wall_s"] > 0 and used["cpu_s"] >= 0
    assert used["peak_rss_kb"] >= 60 * 1024


#: An `rb` that runs one session, which records its pid and sleeps.
_RB_IN_A_SESSION = """
import os, sys
from rtl_buddy.tools.pnr_openroad import _run_session
session = [
    sys.executable,
    "-c",
    "import os, time; "
    "open(os.environ['RB_SESSION_PID'], 'w').write(str(os.getpid())); "
    "time.sleep(600)",
]
with open(os.devnull, "w") as log:
    _run_session(session, stdout=log, env=dict(os.environ), cwd=sys.argv[1])
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for(condition, what: str, seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_terminating_rb_terminates_its_openroad_session(tmp_path):
    pid_file = tmp_path / "session.pid"
    rb = subprocess.Popen(
        [sys.executable, "-c", _RB_IN_A_SESSION, str(tmp_path)],
        env={**os.environ, "RB_SESSION_PID": str(pid_file)},
    )
    try:
        _wait_for(
            lambda: pid_file.is_file() and pid_file.read_text().strip(),
            "the session to start",
        )
        session = int(pid_file.read_text())

        rb.terminate()

        assert rb.wait(timeout=60) == 128 + signal.SIGTERM
        _wait_for(lambda: not _alive(session), "the session to end", 30)
    finally:
        rb.kill()
        rb.wait()


def test_gr_overflow_is_read_from_the_last_usage_table():
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    table = (
        "[INFO GRT-0096] Final congestion report:\n"
        "Layer         Resource        Demand        Usage (%)    Max H / Max V / Total Congestion\n"
        "met1            100638          5154            5.12%             0 /  0 /  0\n"
        "Total           285404         11578            4.06%             {h} /  0 /  {t}\n"
    )
    log = table.format(h=3, t=7) + "...\n" + table.format(h=0, t=2)
    assert OpenRoadPnr._parse_gr_overflow(None, log) == 2
    assert OpenRoadPnr._parse_gr_overflow(None, "no table") is None


def _stage_driver(monkeypatch):
    """An RtlBuddy whose P&R runner records the stage options each run receives."""
    from test_pnr_plan import _Locks

    from rtl_buddy.rtl_buddy import RtlBuddy
    from rtl_buddy.runner.pnr_results import PnrPassResults

    seen = {}

    class _Runner:
        def __init__(self, *, name, **kw):
            self.name = name
            seen[name] = {k: kw.get(k) for k in ("stop_after", "resume", "resume_from")}

        def run(self):
            return PnrPassResults(name=f"{self.name}/results")

    monkeypatch.setattr("rtl_buddy.rtl_buddy.PnrRunner", _Runner)
    rb = RtlBuddy.__new__(RtlBuddy)
    rb.root_cfg = None
    rb._artifact_locks = _Locks()
    return rb, seen


def test_stage_options_reach_the_named_run(tmp_path, monkeypatch):
    from test_pnr_plan import _run, _suite

    suite = _suite(tmp_path / "pnr.yaml", _run("a"), _run("b"))
    rb, seen = _stage_driver(monkeypatch)

    rb._do_pnr_suite(
        suite,
        pnr_name="b",
        stop_after="detailed_route",
        resume="latest",
        resume_from="detailed_route",
    )

    assert seen == {
        "b": {
            "stop_after": "detailed_route",
            "resume": "latest",
            "resume_from": "detailed_route",
        }
    }


@pytest.mark.parametrize(
    ("name", "options", "expected"),
    [
        (None, {"stop_after": "cts"}, "single named pnr run"),
        ("a", {"resume": "latest"}, "go together"),
        ("a", {"stop_after": "finish"}, "--stop-after finish"),
    ],
)
def test_stage_options_are_checked_before_any_run(
    tmp_path, monkeypatch, name, options, expected
):
    from test_pnr_plan import _run, _suite

    suite = _suite(tmp_path / "pnr.yaml", _run("a"))
    rb, seen = _stage_driver(monkeypatch)

    with pytest.raises(FatalRtlBuddyError, match=re.escape(expected)):
        rb._do_pnr_suite(suite, pnr_name=name, **options)
    assert seen == {}


def test_rb_pnr_takes_the_stage_options():
    import typer

    from rtl_buddy.rtl_buddy import RtlBuddy

    group = typer.main.get_command(RtlBuddy(name="test_pnr_stages").app)
    opts = {o for p in group.commands["pnr"].params for o in p.opts}
    assert {"--stop-after", "--resume", "--from"} <= opts


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("latest", "latest"),
        ("20261019T120000-123", "20261019T120000-123"),
        ("saved", "{sub}/saved"),
        ("../saved/20261019T120000-123", "{root}/saved/20261019T120000-123"),
    ],
)
def test_rb_pnr_resolves_a_resume_directory_against_the_invocation_directory(
    minimal_project, monkeypatch, spec, expected
):
    """`latest` and run ids name runs under the run's checkpoints; anything else is a directory relative to where rb was invoked, not to the project root."""
    from typer.testing import CliRunner

    from rtl_buddy.rtl_buddy import RtlBuddy

    root = minimal_project
    (root / "pnr.yaml").write_text("rtl-buddy-filetype: pnr_config\nruns: []\n")
    sub = root / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    seen = {}

    def _capture(self, suite, **kwargs):
        seen.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(RtlBuddy, "_do_pnr_suite", _capture)
    rb = RtlBuddy(name="test_pnr_stages")
    try:
        result = CliRunner().invoke(
            rb.app,
            ["pnr", "r", "-c", "../pnr.yaml", "--resume", spec, "--from", "cts"],
        )
    finally:
        rb._artifact_locks.release_all()

    assert "resume" in seen, result.output
    resolved = expected.format(sub=sub, root=root)
    if os.path.isabs(resolved):
        assert os.path.normpath(seen["resume"]) == resolved
    else:
        assert seen["resume"] == resolved


def test_locate_run_never_reads_the_working_directory(tmp_path, monkeypatch):
    artefacts = tmp_path / "artefacts" / "r"
    run = artefacts / "checkpoints" / "20261019T120000-1"
    run.mkdir(parents=True)
    (artefacts / "checkpoints" / "latest").symlink_to(run.name)
    elsewhere = tmp_path / "elsewhere"
    for name in ("latest", "20261019T120000-1", "saved"):
        (elsewhere / name).mkdir(parents=True)
    monkeypatch.chdir(elsewhere)

    assert pnr_stages.locate_run(str(artefacts), "latest") == str(run)
    assert pnr_stages.locate_run(str(artefacts), "20261019T120000-1") == str(run)
    assert pnr_stages.locate_run(str(artefacts), "saved") is None
    assert pnr_stages.locate_run(str(artefacts), str(elsewhere / "saved")) == str(
        elsewhere / "saved"
    )


def test_result_rows_carry_the_stage_fields():
    from rtl_buddy.rtl_buddy import RtlBuddy
    from rtl_buddy.runner.pnr_results import PnrPassResults

    res = PnrPassResults(
        name="r/results",
        fields={
            "routed": False,
            "stopped_after": "global_route",
            "global_route_overflow": 0,
            "resumed_from": {"run_id": "x", "from": "detailed_route"},
        },
    )
    row = RtlBuddy._pnr_result_row(None, {"pnr_name": "r", "results": res})
    assert row["routed"] is False
    assert row["stopped_after"] == "global_route"
    assert row["global_route_overflow"] == 0
    assert row["resumed_from"]["run_id"] == "x"


def test_resume_and_stop_messages_read_plainly():
    from rtl_buddy.logging_utils import _human_message

    refused = _human_message(
        "pnr.resume_refused",
        {"pnr": "p", "resume": "latest", "from_stage": "cts", "reason": "why"},
    )
    assert refused == 'P&R "p": --resume latest --from cts refused: why'
    stopped = _human_message(
        "pnr.stopped", {"pnr": "p", "stage": "cts", "checkpoint": "d/03_cts"}
    )
    assert "stopped after cts" in stopped and "not routed" in stopped
