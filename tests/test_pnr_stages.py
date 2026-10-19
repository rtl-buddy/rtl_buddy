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


def test_global_route_checkpoint_follows_hold_repair_and_precedes_hooks(tmp_path):
    text = _render(tmp_path, CHECKPOINT_STAGES).read_text()
    end = text.index("rb::ckpt::stage_end global_route\n")
    assert text.index("global_route -congestion_iterations") < end
    assert end < text.index('puts ">>> Detail route"')


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
    assert "--stop-after cts" in res.results["desc"]


def test_a_staged_session_that_exits_early_fails_the_chain(tmp_path, monkeypatch):
    backend, artefacts = _staged(
        tmp_path, monkeypatch, env={"RB_STUB_EXIT": "clock_tree_synthesis"}
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
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
    run_dir = _only_run(artefacts)
    ended = {e["stage"]: e for e in _events(run_dir) if e["event"] == "stage_end"}
    assert ended["global_route"]["status"] == "error"
    assert "checkpoint" not in ended["global_route"]


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
