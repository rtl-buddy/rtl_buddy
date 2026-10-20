"""Tests that a P&R run whose OpenROAD session exits 0 before the end of its flow fails.

Tcl the flow sources can end the script with exit code 0: an `exit`, or a `return -level 2` from a sourced file. Every session rb runs must reach its end marker (`>>> DONE`, `>>> End of stage NN_<stage>`, `>>> STOPPED after <stage>`), or the run is a FAIL with `fail_stage: incomplete` that names the last step in the log. The generated scripts run under the stub OpenROAD of `test_pnr_checkpoints`.
"""

from dataclasses import replace
from importlib.resources import files

import pytest
from test_pnr_checkpoints import _backend, _events, _pnr_cfg, _runs
from test_pnr_repair import _PR, _repair_backend

from rtl_buddy.runner.xfail import xfail_refusal
from rtl_buddy.tools import pnr_openroad, pnr_stages
from rtl_buddy.tools.pnr_openroad import _last_logged_step

#: Where each hook ends the session: its PDK field (None for `pin-constraints`) and the step the log shows last.
_HOOKS = {
    "platform-tcl": ("platform_tcl", "Platform Tcl (platform-tcl)"),
    "layer-rc-tcl": ("layer_rc_tcl", "Layer RC (layer-rc-tcl)"),
    "tracks-tcl": ("tracks_tcl", "Routing tracks (tracks-tcl)"),
    "tapcell-tcl": ("tapcell_tcl", "Tap and endcap cells (tapcell-tcl)"),
    "pdn-config": ("pdn_config", "Power distribution network"),
    "pin-constraints": (None, "IO pin placement"),
}

_ENDINGS = {"exit": "exit 0\n", "return-level-2": "return -level 2\n"}

#: `(stages, checkpoints)` of the runs each hook is tried in: one session without and with progress events, and a staged run.
_MODES = {
    "unbroken": (False, None),
    "unbroken-with-checkpoints": (False, True),
    "staged": (True, None),
}


def _ending_early(tmp_path, monkeypatch, hook, ending, mode, before=""):
    """A backend whose `hook` file prints `before`, then ends the session with `ending`."""
    field_name, _step = _HOOKS[hook]
    stages, checkpoints = _MODES[mode]
    script = tmp_path / "pdk" / f"{hook}.tcl"
    script.parent.mkdir(exist_ok=True)
    script.write_text(before + _ENDINGS[ending])
    kw = {"stages": stages}
    if not stages:
        kw["checkpoints"] = (
            pnr_openroad.pnr_checkpoints.CHECKPOINT_STAGES if checkpoints else None
        )
    pnr_cfg = _pnr_cfg(tmp_path, **kw)
    pdk_fields = {}
    if field_name is None:
        pnr_cfg = replace(pnr_cfg, pin_constraints=str(script))
    else:
        pdk_fields[field_name] = f"pdk/{script.name}"
    if field_name != "pdn_config":
        pdn = tmp_path / "pdk" / "pdn.tcl"
        pdn.write_text("# power grid\n")
        pdk_fields["pdn_config"] = "pdk/pdn.tcl"
    return _backend(tmp_path, monkeypatch, pnr_cfg=pnr_cfg, pdk_fields=pdk_fields)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("ending", _ENDINGS)
@pytest.mark.parametrize("hook", _HOOKS)
def test_a_hook_that_ends_the_session_fails_the_run(
    tmp_path, monkeypatch, hook, ending, mode
):
    backend, artefacts = _ending_early(tmp_path, monkeypatch, hook, ending, mode)

    res = backend.run()

    assert res.results["result"] == "FAIL", res.results
    assert res.results["fail_stage"] == "incomplete"
    desc = res.results["desc"]
    assert "the flow did not finish" in desc
    assert f"the last step in the log is '{_HOOKS[hook][1]}'" in desc
    if mode == "staged":
        assert desc.startswith("stage 01_floorplan: ")
    assert not list(artefacts.glob("*.routed.*"))
    assert "routed" not in res.results


#: Lines a flow prints before it ends early: a design check each would fail on its own.
_BEFORE = {
    "block-power-error": (
        'puts "RB-BLOCK-POWER-ERROR: u_blk/VDDB is not on a parent VDD strap"\n'
    ),
    "dont-use-hit": 'puts "RB-DONT-USE-VIOLATION: u1 AND2_X1 AND2_X1"\n',
}


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("before", _BEFORE)
def test_a_design_failure_does_not_hide_that_the_flow_ended_early(
    tmp_path, monkeypatch, before, mode
):
    backend, _ = _ending_early(
        tmp_path, monkeypatch, "pin-constraints", "exit", mode, before=_BEFORE[before]
    )

    res = backend.run()

    assert res.results["result"] == "FAIL", res.results
    assert res.results["fail_stage"] == "incomplete"
    assert "the flow did not finish" in res.results["desc"]
    assert xfail_refusal(res.results) == "incomplete"


def test_the_message_names_the_last_command_when_progress_is_recorded(
    tmp_path, monkeypatch
):
    backend, artefacts = _backend(
        tmp_path, monkeypatch, env={"RB_STUB_EXIT": "detailed_route"}
    )

    res = backend.run()

    assert res.results["fail_stage"] == "incomplete"
    assert "the last step in the log is 'Detail route'" in res.results["desc"]
    assert "(last command: detailed_route, running)" in res.results["desc"]
    assert res.results["last_step"] == {"step": "detailed_route", "status": "running"}
    assert "'>>> DONE' is not in the log" in res.results["desc"]


def test_a_staged_run_names_the_session_not_an_earlier_one(tmp_path, monkeypatch):
    backend, artefacts = _backend(
        tmp_path,
        monkeypatch,
        stages=True,
        env={"RB_STUB_EXIT": "clock_tree_synthesis"},
    )

    res = backend.run()

    assert res.results["fail_stage"] == "incomplete"
    desc = res.results["desc"]
    assert desc.startswith("stage 03_cts: the flow did not finish")
    assert "'>>> End of stage 03_cts' is not in the log" in desc
    assert "the last step in the log is 'Clock tree synthesis'" in desc
    assert "last command: clock_tree_synthesis, running" in desc
    (run_dir,) = _runs(artefacts)
    ended = {e["stage"]: e for e in _events(run_dir) if e["event"] == "stage_end"}
    assert ended["cts"]["status"] == "error"
    assert not (run_dir / "04_global_route.log").exists()


@pytest.mark.parametrize("stages", [False, True])
def test_a_flow_that_ends_before_its_last_stage_marker_fails(
    tmp_path, monkeypatch, stages
):
    """`finish` and a one-session run end on `>>> DONE`, not on a stage marker."""
    backend, artefacts = _backend(
        tmp_path, monkeypatch, stages=stages, env={"RB_STUB_EXIT": "filler_placement"}
    )

    res = backend.run()

    assert res.results["fail_stage"] == "incomplete"
    desc = res.results["desc"]
    assert "the last step in the log is 'Fill insertion'" in desc
    assert desc.startswith("stage 07_finish: ") == stages


def test_a_done_marker_printed_early_does_not_end_the_flow(tmp_path, monkeypatch):
    done_early = tmp_path / "pdk" / "platform.tcl"
    done_early.parent.mkdir()
    done_early.write_text(f'puts "{pnr_stages.FLOW_DONE_MARKER}"\n')
    pdn = tmp_path / "pdk" / "pdn.tcl"
    pdn.write_text("exit 0\n")
    backend, _ = _backend(
        tmp_path,
        monkeypatch,
        checkpoints=None,
        pdk_fields={"platform_tcl": "pdk/platform.tcl", "pdn_config": "pdk/pdn.tcl"},
    )

    res = backend.run()

    assert res.results["result"] == "FAIL", res.results
    assert res.results["fail_stage"] == "incomplete"
    assert res.results["desc"] == (
        "the flow did not finish: OpenROAD exited with code 0 before the end of the "
        "flow ('>>> DONE' is not the last step in the log); "
        "the last step in the log is 'Power distribution network'"
    )


def test_a_resumed_finish_that_printed_done_early_fails(tmp_path, monkeypatch):
    first, _ = _backend(tmp_path, monkeypatch, stages=True, stop_after="detailed_route")
    assert first.run().results["result"] == "PASS"

    backend, _ = _backend(
        tmp_path,
        monkeypatch,
        stages=True,
        resume="latest",
        resume_from="finish",
        env={"RB_STUB_DONE_EARLY": "filler_placement", "RB_STUB_EXIT": "write_db"},
    )
    res = backend.run()

    assert res.results["fail_stage"] == "incomplete"
    assert res.results["desc"].startswith("stage 07_finish: ")
    assert "'>>> DONE' is not the last step in the log" in res.results["desc"]


def test_a_resumed_session_that_ends_early_fails(tmp_path, monkeypatch):
    first, _ = _backend(tmp_path, monkeypatch, stages=True, stop_after="global_route")
    assert first.run().results["result"] == "PASS"

    backend, artefacts = _backend(
        tmp_path,
        monkeypatch,
        stages=True,
        resume="latest",
        resume_from="detailed_route",
        env={"RB_STUB_EXIT": "detailed_route"},
    )
    res = backend.run()

    assert res.results["fail_stage"] == "incomplete"
    assert res.results["desc"].startswith("stage 05_detailed_route: ")


def test_a_resumed_finish_that_ends_early_fails(tmp_path, monkeypatch):
    first, _ = _backend(tmp_path, monkeypatch, stages=True, stop_after="detailed_route")
    assert first.run().results["result"] == "PASS"

    backend, artefacts = _backend(
        tmp_path,
        monkeypatch,
        stages=True,
        resume="latest",
        resume_from="finish",
        env={"RB_STUB_EXIT": "filler_placement"},
    )
    res = backend.run()

    assert res.results["fail_stage"] == "incomplete"
    assert res.results["desc"].startswith("stage 07_finish: ")


@pytest.mark.parametrize("stages", [False, True])
def test_a_stopped_run_that_ends_early_names_the_last_step(
    tmp_path, monkeypatch, stages
):
    backend, artefacts = _backend(
        tmp_path,
        monkeypatch,
        stages=stages,
        checkpoints=None,
        stop_after="cts",
        env={"RB_STUB_EXIT": "repair_timing"},
    )

    res = backend.run()

    assert res.results["fail_stage"] == "incomplete"
    last = (
        "the last step in the log is 'Post-CTS timing repair' "
        "(last command: repair_timing, running)"
    )
    expected = {
        False: (
            "the flow did not finish: OpenROAD exited with code 0 before "
            f"--stop-after cts finished ('>>> STOPPED after cts' is not in the log); {last}"
        ),
        True: (
            "stage 03_cts: the flow did not finish: OpenROAD exited with code 0 before "
            f"the end of the stage ('>>> End of stage 03_cts' is not in the log); {last}"
        ),
    }
    assert res.results["desc"] == expected[stages]


#: A post-route repair block whose one round repairs while setup misses its margin.
_ROUND = {"post_route": {**_PR, "rounds": 1}}


@pytest.mark.parametrize("stages", [False, True])
@pytest.mark.parametrize(
    ("command", "session", "step"),
    [
        ("extract_parasitics", "round 1 extraction", "round 1: extraction"),
        ("remove_fillers", "round 1 repair", "round 1 of 1: repair"),
    ],
)
def test_a_post_route_repair_session_that_ends_early_names_it(
    tmp_path, monkeypatch, stages, command, session, step
):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        stages=stages,
        env={"RB_STUB_SETUP": "-0.2", "RB_STUB_EXIT": command},
        **_ROUND,
    )
    harness = tmp_path / "harness.tcl"
    harness.write_text(
        "proc extract_parasitics {args} { rb_stub extract_parasitics }\n"
        + harness.read_text()
    )

    res = backend.run()

    assert res.results["fail_stage"] == "incomplete"
    assert res.results["desc"].startswith(
        "stage 06_post_route_repair: the flow did not finish: OpenROAD exited with "
        f"code 0 before the end of {session} "
        f"('>>> End of post-route repair {session}' is not in the log); "
        f"the last step in the log is 'Post-route repair {step}"
    )
    (run_dir,) = _runs(artefacts)
    assert not (run_dir / "07_finish.log").exists()


@pytest.mark.parametrize("stages", [False, True])
def test_finish_after_post_route_repair_that_ends_early_names_its_stage(
    tmp_path, monkeypatch, stages
):
    backend, _ = _repair_backend(
        tmp_path,
        monkeypatch,
        stages=stages,
        env={"RB_STUB_SETUP": "-0.2", "RB_STUB_EXIT": "filler_placement"},
        **_ROUND,
    )

    res = backend.run()

    assert res.results["fail_stage"] == "incomplete"
    assert res.results["desc"] == (
        "stage 07_finish: the flow did not finish: OpenROAD exited with code 0 "
        "before the end of the flow ('>>> DONE' is not in the log); the last step in "
        "the log is 'Fill insertion' (last command: filler_placement, running)"
    )


def test_a_one_session_run_with_post_route_repair_that_ends_before_its_rounds(
    tmp_path, monkeypatch
):
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        env={"RB_STUB_SETUP": "-0.2", "RB_STUB_EXIT": "clock_tree_synthesis"},
        **_ROUND,
    )

    res = backend.run()

    assert res.results["fail_stage"] == "incomplete"
    desc = res.results["desc"]
    assert desc.startswith(
        "stage 05_detailed_route: the flow did not finish: OpenROAD exited with code "
        "0 before the end of the stage ('>>> End of stage 05_detailed_route' is not "
        "in the log); the last step in the log is "
    )
    assert desc.endswith("(last command: clock_tree_synthesis, running)")
    (run_dir,) = _runs(artefacts)
    assert not (run_dir / "06_post_route_repair.r1.extract.log").exists()


@pytest.mark.parametrize("stop_after", [None, "cts"])
@pytest.mark.parametrize("stages", [False, True])
def test_runs_that_end_on_purpose_are_not_incomplete(
    tmp_path, monkeypatch, stages, stop_after
):
    backend, _ = _backend(tmp_path, monkeypatch, stages=stages, stop_after=stop_after)

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert "fail_stage" not in res.results


def test_an_xfail_marker_does_not_excuse_an_incomplete_flow(tmp_path, monkeypatch):
    backend, _ = _backend(tmp_path, monkeypatch, env={"RB_STUB_EXIT": "global_route"})

    res = backend.run()

    assert xfail_refusal(res.results) == "incomplete"


def test_the_flow_template_ends_on_the_done_marker_after_every_output():
    template = (
        files(pnr_openroad._TEMPLATE_PACKAGE)
        .joinpath(pnr_openroad._TEMPLATE_FILE)
        .read_text()
    )
    puts = [line for line in template.splitlines() if line.startswith("puts ")]

    assert puts[-1] == f'puts "{pnr_stages.FLOW_DONE_MARKER}"'
    assert template.rstrip().endswith("exit 0")
    assert template.index("{{ harden_block }}") < template.rindex(
        pnr_stages.FLOW_DONE_MARKER
    )
    assert template.index("write_db      $OUT_DIR") < template.rindex(
        pnr_stages.FLOW_DONE_MARKER
    )


def test_the_last_step_is_the_last_announced_line():
    log = "\n".join(
        [
            ">>> Reading netlist",
            ">>> Macro placement",
            ">>>   placed u_ram at (1, 2) DBU",
            ">>> rb: log of inherited stage floorplan not readable: x",
            "[INFO GPL-0001] noise",
        ]
    )

    assert _last_logged_step(log) == "Macro placement"
    assert _last_logged_step("[INFO] no step\n") is None
