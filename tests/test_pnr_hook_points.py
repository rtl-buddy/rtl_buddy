"""Tests for the `rb pnr` hook points around CTS, timing repair, extraction, the final reports and stage ends.

Pinned behaviour:

- without the points, every script rb renders is as before: setting them adds only their blocks;
- each point belongs to the stage it runs in, runs at the same place in staged and one-session runs, and runs again with its stage on a resume;
- a stage's checkpoint is written after its `stage-end` hooks, which see the stage and the run's mode, and a failing one fails the run naming the stage;
- each session that writes a SPEF runs the `after-extraction` hooks on it before anything reads it;
- the `after-reports` hooks run on the final parasitics, before the outputs are written.

Sessions run under the stub OpenROAD harness of `test_pnr_checkpoints`.
"""

import re
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

import pytest
from test_pnr_blocks import _write
from test_pnr_checkpoints import _backend, _events, _pnr_cfg, _runs
from test_pnr_hooks import _RUN_ORDER, _announced, _hook_files, _tcl_run
from test_pnr_repair import _logged, _with_repair

from rtl_buddy.config.pnr import CHECKPOINT_STAGES, HOOK_POINTS
from rtl_buddy.config.pnr_repair import PnrMode
from rtl_buddy.runner.pnr_results import PnrFailResults, PnrPassResults
from rtl_buddy.tools import pnr_stages

#: The hook points added beside the original eleven.
_NEW_POINTS = (
    "before-cts",
    "before-repair-clock-nets",
    "after-timing-repair",
    "after-cts-repair",
    "after-global-route-repair",
    "after-extraction",
    "after-reports",
    "stage-end",
)
_OLD_POINTS = tuple(p for p in HOOK_POINTS if p not in _NEW_POINTS)

#: Post-route repair the stub rounds run twice: round 0 fails setup, round 1 improves, round 2 meets it.
_ROUNDS = {"setup-margin": 0.1, "rounds": 2}
_ROUNDS_ENV = {
    "RB_STUB_SETUP": "-0.2 -0.1 0.05",
    "RB_STUB_SETUP_ss": "-0.2 -0.1 0.05",
    "RB_STUB_SETUP_ff": "0.3 0.3 0.3",
}

#: Stubs that make the finish session's SPEF a file too, and print what each `read_spef` reads.
_SPEF_STUBS = r"""
proc write_spef {path} {
    rb_write $path "spef [expr {[rb_round] eq "" ? "final" : "r[rb_round]"}]"
}
proc read_spef {args} {
    set path [lindex $args end]
    set fh [open $path]
    set text [string trim [read $fh]]
    close $fh
    puts "stub: read_spef [lrange $args 0 end-1] [file tail $path]: $text"
}
"""


def _hooks(tmp_path, points, text=None) -> dict:
    """One hook file per point, as `_hook_files` writes them unless `text` is given."""
    paths = _hook_files(tmp_path, points)
    if text is not None:
        for path in paths.values():
            _write(Path(path), text)
    return {point: (path,) for point, path in paths.items()}


def _run_backend(
    tmp_path,
    monkeypatch,
    hooks,
    *,
    stages=False,
    checkpoints=CHECKPOINT_STAGES,
    mode=PnrMode.CLOSURE,
    post_route=None,
    corners=None,
    env=None,
    spef_stubs=False,
    platform=None,
    **options,
):
    """A stub-harness backend with `hooks`, a PDK with `rcx-rules`, `post_route` as its post-route repair, and the platform fields in `platform`."""
    cfg = replace(
        _pnr_cfg(tmp_path, checkpoints, stages=stages), mode=mode, hooks=hooks
    )
    backend, artefacts = _backend(
        tmp_path, monkeypatch, pnr_cfg=cfg, stages=stages, env=env, **options
    )
    _with_repair(
        backend, tmp_path, post_route=post_route, corners=corners, platform=platform
    )
    if spef_stubs:
        harness = tmp_path / "harness.tcl"
        harness.write_text(
            harness.read_text().replace(
                "source [lindex $argv 0]", _SPEF_STUBS + "source [lindex $argv 0]"
            )
        )
    return backend, artefacts


def _announcements(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(">>> Hook ")]


# Unset points


def _scripts(artefacts) -> dict[str, str]:
    """Every script the run wrote, by file name, with its run directory and test root blanked."""
    root = str(artefacts.parents[1])
    paths = [artefacts / "pnr.tcl"]
    for run_dir in _runs(artefacts):
        paths += sorted(run_dir.glob("*.tcl"))
    return {
        path.name: re.sub(
            r"\d{8}T\d{6}-\d+", "<run>", path.read_text().replace(root, "<root>")
        )
        for path in paths
        if not path.name.endswith(".state.tcl")
    }


#: A new point's block: a `before-cts` one ends a one-session run's `place` stage first, unless `repair_clock_inverters` follows and that ends it, an `after-extraction` one names its SPEF, an `after-timing-repair` one sets and unsets its repair and round.
_NEW_BLOCK_RE = re.compile(
    r"(?:\n|(?<=\n)set rb_repair \w+\n(?:set rb_repair_round \d+\n)?)"
    r"(?:rb::ckpt::checkpoint_for clock_tree_synthesis enter\n"
    r"(?![^\n]*\n[^\n]*\n\nrepair_clock_inverters\n))?"
    r"(?:set rb_spef [^\n]*\n)?"
    r'puts ">>> Hook (?:' + "|".join(map(re.escape, _NEW_POINTS)) + r")[^\n]*\n"
    r"rb::hook::(?:run|stage_end) [^\n]*\n"
    r"(?:unset -nocomplain rb_repair(?: rb_repair_round)?\n)?"
)


#: Ends the final reports when `after-reports` or `stage-end` hooks follow them.
_REPORTS_END = '\nputs ">>> End of final reports"'


def _without_new_blocks(text: str) -> str:
    procs = (
        "\n"
        + files("rtl_buddy.pnr").joinpath("stage_end_hook.tcl").read_text()
        + "set rb_mode closure\n"
    )
    text = text.replace(procs, "").replace(_REPORTS_END, "")
    return _NEW_BLOCK_RE.sub("", text)


@pytest.mark.parametrize("stages", [True, False], ids=["staged", "one-session"])
def test_the_new_points_add_only_their_blocks_to_every_script(
    tmp_path, monkeypatch, stages
):
    """Every session script, the post-route rounds' and the electrical round's included, differs only by the blocks of the points set, so a run without them renders as before."""
    scripts = {}
    for name, points in (("old", _OLD_POINTS), ("all", HOOK_POINTS)):
        root = tmp_path / name
        root.mkdir()
        backend, artefacts = _run_backend(
            root,
            monkeypatch,
            _hooks(tmp_path, points),
            stages=stages,
            checkpoints=None,
            post_route={**_ROUNDS, "electrical-repair": {"slew-margin": 10}},
            corners=["ss", "ff"],
            env={**_ROUNDS_ENV, "RB_STUB_SLEW": "3 2 1 0", "RB_STUB_CAP": "0 0 0 0"},
        )
        assert isinstance(backend.run(), PnrPassResults)
        scripts[name] = _scripts(artefacts)

    assert scripts["all"].keys() == scripts["old"].keys()
    assert "06_post_route_repair.r3.repair.tcl" in scripts["all"]
    for name, text in scripts["all"].items():
        assert _without_new_blocks(text) == scripts["old"][name], name
    if stages:
        assert "rb::hook::run after-extraction" in scripts["all"]["07_finish.tcl"]
        extract = scripts["all"]["06_post_route_repair.r3.extract.tcl"]
        assert "rb::hook::run after-extraction" in extract


def test_without_the_stage_end_hooks_a_detailed_route_ends_on_leaving_the_router(
    tmp_path,
):
    """Only `stage-end` hooks move a one-session run's detailed-route checkpoint, to after them."""
    from rtl_buddy.tools import pnr_checkpoints

    plain = pnr_checkpoints.render_tcl_block(
        "/run", CHECKPOINT_STAGES, order=pnr_stages.TEMPLATE_STAGES
    )
    flow_ended = pnr_checkpoints.render_tcl_block(
        "/run",
        CHECKPOINT_STAGES,
        order=pnr_stages.TEMPLATE_STAGES,
        flow_ended=("detailed_route",),
    )
    assert "detailed_route {detailed_route leave}" in plain
    assert flow_ended == plain.replace(" detailed_route {detailed_route leave}", "")


# Stage ownership and flow position

#: The hook files each staged session runs, in order; `session` hooks aside.
_STAGE_HOOKS = {
    "01_floorplan": [
        ("after-floorplan", None),
        ("after-pdn", None),
        ("after-pins", None),
        ("stage-end", "floorplan"),
    ],
    "02_place": [
        ("after-global-placement", None),
        ("after-repair-design", None),
        ("stage-end", "place"),
    ],
    "03_cts": [
        ("before-cts", None),
        ("before-repair-clock-nets", None),
        ("after-cts", None),
        ("after-timing-repair", "post_cts"),
        ("after-cts-repair", None),
        ("stage-end", "cts"),
    ],
    "04_global_route": [
        ("after-global-route", None),
        ("after-timing-repair", "global_route"),
        ("after-global-route-repair", None),
        ("stage-end", "global_route"),
    ],
    "05_detailed_route": [
        ("before-detailed-route", None),
        ("stage-end", "detailed_route"),
    ],
    "07_finish": [
        ("after-extraction", None),
        ("before-reports", None),
        ("after-reports", None),
        ("stage-end", "finish"),
    ],
}


def test_each_point_runs_in_its_stage_and_where_a_one_session_run_runs_it(
    tmp_path, monkeypatch
):
    points = [p for p in HOOK_POINTS if p != "macro-placement"]
    hooks = _hooks(tmp_path, points)
    order = [(p, s) for p, s in _RUN_ORDER if p in points and p != "session"]

    logs = {}
    for stages in (False, True):
        root = tmp_path / ("staged" if stages else "one")
        root.mkdir()
        backend, artefacts = _run_backend(root, monkeypatch, hooks, stages=stages)
        assert isinstance(backend.run(), PnrPassResults)
        logs[stages] = (artefacts / "pnr.log").read_text()
        if stages:
            (run_dir,) = _runs(artefacts)

    for text in logs.values():
        announced = [a for a in _announcements(text) if "Hook session:" not in a]
        assert announced == [f"{_announced(p, s)} {hooks[p][0]}" for p, s in order]
    for name, expected in _STAGE_HOOKS.items():
        script = (run_dir / f"{name}.tcl").read_text()
        log = (run_dir / f"{name}.log").read_text()
        announced = [a for a in _announcements(log) if "Hook session:" not in a]
        assert announced == [f"{_announced(p, s)} {hooks[p][0]}" for p, s in expected]
        stage = name[3:]
        # The stage's checkpoint follows its `stage-end` hooks.
        if stage != "finish":
            assert script.index(f"rb::hook::stage_end {stage} ") < script.index(
                f"rb::ckpt::stage_end {stage}\n"
            )
    finish = (run_dir / "07_finish.tcl").read_text()
    assert (
        finish.index("write_spef $OUT_DIR/${DESIGN}.routed.spef")
        < finish.index("rb::hook::run after-extraction")
        < finish.index("rb::hook::run before-reports")
        < finish.index('puts ">>> Final reports"')
        < finish.index("rb::hook::run after-reports")
        < finish.index("rb::hook::stage_end finish")
        < finish.index('puts ">>> Write outputs"')
    )


@pytest.mark.parametrize(
    "point, stage",
    [
        ("before-cts", "cts"),
        ("before-repair-clock-nets", "cts"),
        ("after-timing-repair", "cts"),
        ("after-cts-repair", "cts"),
        ("after-global-route-repair", "global_route"),
        ("after-reports", "finish"),
        ("stage-end", "floorplan"),
    ],
)
def test_a_resume_compares_a_hook_only_from_the_stage_that_first_runs_it(point, stage):
    role = "hook_" + point.replace("-", "_")
    assert not pnr_stages.input_read_before(role, stage)
    later = pnr_stages.PNR_STAGES[pnr_stages.PNR_STAGES.index(stage) + 1 :]
    assert all(pnr_stages.input_read_before(role, s) for s in later)


def test_after_extraction_is_first_read_where_the_spef_is_first_written():
    role = "hook_after_extraction"
    with_repair = pnr_stages.PNR_STAGES
    assert not pnr_stages.input_read_before(role, "post_route_repair", with_repair)
    assert pnr_stages.input_read_before(role, "finish", with_repair)
    without = pnr_stages.TEMPLATE_STAGES
    assert not pnr_stages.input_read_before(role, "finish", without)


def test_a_run_resumed_at_cts_runs_the_before_cts_hooks(tmp_path, monkeypatch):
    hooks = _hooks(tmp_path, ("before-cts", "stage-end"))
    backend, artefacts = _run_backend(
        tmp_path, monkeypatch, hooks, stages=True, stop_after="place"
    )
    assert isinstance(backend.run(), PnrPassResults)
    (first,) = _runs(artefacts)
    assert "MARK before-cts" not in (artefacts / "pnr.log").read_text()

    backend, _ = _run_backend(
        tmp_path, monkeypatch, hooks, stages=True, resume="latest", resume_from="cts"
    )
    res = backend.run()

    assert isinstance(res, PnrPassResults), res.results
    second = next(r for r in _runs(artefacts) if r != first)
    cts = (second / "03_cts.log").read_text().splitlines()
    assert cts.index("MARK before-cts") < cts.index("MARK stage-end cts closure")
    # The inherited stages' hooks do not run again.
    assert not (second / "02_place.log").exists()
    assert "MARK stage-end place" not in "\n".join(cts)
    manifest_inputs = (second / "manifest.json").read_text()
    assert '"hook_before_cts"' in manifest_inputs
    assert '"hook_stage_end"' in manifest_inputs


# One-session checkpoints


def _kinds(events) -> list[tuple[str, str]]:
    return [
        (e["event"], e.get("stage") or e.get("point") or e.get("step"))
        for e in events
        if e["event"] in ("hook_end", "hook_begin", "checkpoint_begin")
    ]


def test_a_one_session_checkpoint_follows_its_stage_end_hooks(tmp_path, monkeypatch):
    hooks = _hooks(tmp_path, ("before-cts", "stage-end"))
    backend, artefacts = _run_backend(tmp_path, monkeypatch, hooks)

    assert isinstance(backend.run(), PnrPassResults)
    (run_dir,) = _runs(artefacts)
    kinds = [k for k in _kinds(_events(run_dir)) if k[0] != "hook_begin"]
    assert kinds == [
        ("hook_end", "stage-end"),
        ("checkpoint_begin", "floorplan"),
        ("hook_end", "stage-end"),
        ("checkpoint_begin", "place"),
        ("hook_end", "before-cts"),
        ("hook_end", "stage-end"),
        ("checkpoint_begin", "cts"),
        ("hook_end", "stage-end"),
        ("checkpoint_begin", "global_route"),
        ("hook_end", "stage-end"),
        ("checkpoint_begin", "detailed_route"),
        ("hook_end", "stage-end"),
    ]
    ended = [e["stage"] for e in _events(run_dir) if e["event"] == "stage_end"]
    assert ended == list(pnr_stages.TEMPLATE_STAGES)


def test_the_place_checkpoint_precedes_the_before_cts_hooks(tmp_path, monkeypatch):
    """The `place` stage, which ends on entering `clock_tree_synthesis` without them, ends before the hooks; the detailed route still ends on leaving the router."""
    hooks = _hooks(tmp_path, ("before-cts",))
    backend, artefacts = _run_backend(
        tmp_path,
        monkeypatch,
        hooks,
        platform={"detailed_route_antenna_repair": False},
    )

    assert isinstance(backend.run(), PnrPassResults)
    (run_dir,) = _runs(artefacts)
    kinds = _kinds(_events(run_dir))
    assert kinds.index(("checkpoint_begin", "place")) < kinds.index(
        ("hook_begin", "before-cts")
    )
    text = (artefacts / "pnr.tcl").read_text()
    assert "detailed_route {detailed_route leave}" in text
    assert "rb::ckpt::stage_end detailed_route" not in text


# Stage end


def test_stage_end_hooks_see_the_stage_and_a_probe_s_mode(tmp_path, monkeypatch):
    hooks = _hooks(tmp_path, ("stage-end", "after-global-route-repair"))
    backend, artefacts = _run_backend(
        tmp_path, monkeypatch, hooks, stages=True, mode=PnrMode.PROBE
    )

    assert isinstance(backend.run(), PnrPassResults)
    marks = [
        line
        for line in (artefacts / "pnr.log").read_text().splitlines()
        if line.startswith("MARK ")
    ]
    assert marks == [
        "MARK stage-end floorplan probe",
        "MARK stage-end place probe",
        "MARK stage-end cts probe",
        "MARK after-global-route-repair",
        "MARK stage-end global_route probe",
    ]


_FAIL_AT_CTS = (
    'if {$rb_stage eq "cts"} { error "grid broken" }\nputs "MARK $rb_stage"\n'
)


@pytest.mark.parametrize(
    "stages, checkpoints",
    [(False, None), (False, CHECKPOINT_STAGES), (True, CHECKPOINT_STAGES)],
    ids=["one-session", "one-session-checkpoints", "staged"],
)
def test_a_failing_stage_end_hook_fails_the_run_naming_the_stage(
    tmp_path, monkeypatch, stages, checkpoints
):
    hooks = _hooks(tmp_path, ("stage-end",), _FAIL_AT_CTS)
    path = hooks["stage-end"][0]
    backend, artefacts = _run_backend(
        tmp_path, monkeypatch, hooks, stages=stages, checkpoints=checkpoints
    )

    res = backend.run()

    assert isinstance(res, PnrFailResults)
    desc = res.results["desc"]
    assert f"Stage cts: Hook stage-end ({path}): grid broken" in desc
    if stages:
        assert desc.startswith("stage 03_cts: ")
    log = (artefacts / "pnr.log").read_text()
    assert "MARK place" in log and "MARK cts" not in log
    assert ">>> Global route" not in log
    if checkpoints:
        (run_dir,) = _runs(artefacts)
        written = [
            e["stage"] for e in _events(run_dir) if e["event"] == "checkpoint_begin"
        ]
        assert written == ["floorplan", "place"]


@pytest.mark.parametrize(
    "point, body, named",
    [
        (
            "stage-end",
            'if {$rb_stage eq "post_route_repair"} { error "grid broken" }\n',
            "Stage post_route_repair: Hook stage-end",
        ),
        (
            "after-timing-repair",
            'if {$rb_repair eq "post_route"} { error "grid broken" }\n',
            "Hook after-timing-repair",
        ),
    ],
)
def test_a_failing_hook_in_a_repair_round_fails_the_run(
    tmp_path, monkeypatch, point, body, named
):
    """A repair round whose session fails is otherwise set aside, and the stage keeps the best round."""
    hooks = _hooks(tmp_path, (point,), body)
    backend, artefacts = _run_backend(
        tmp_path,
        monkeypatch,
        hooks,
        stages=True,
        post_route=_ROUNDS,
        env=_ROUNDS_ENV,
    )
    logged = _logged(monkeypatch)

    res = backend.run()

    assert isinstance(res, PnrFailResults)
    desc = res.results["desc"]
    assert desc.startswith("stage 06_post_route_repair: ")
    assert named in desc
    assert not any(e == "pnr.post_route_round_failed" for _l, e, _f in logged)
    (run_dir,) = _runs(artefacts)
    assert not (run_dir / "06_post_route_repair.r1.extract.log").exists()
    assert not (run_dir / "07_finish.log").exists()


def test_the_stage_end_proc_leads_the_error_with_the_stage(tmp_path):
    hook = _write(tmp_path / "bad.tcl", 'error "boom"\n')
    pnr = files("rtl_buddy.pnr")
    procs = pnr.joinpath("hooks.tcl").read_text()
    procs += pnr.joinpath("stage_end_hook.tcl").read_text()
    done = _tcl_run(
        tmp_path,
        procs + f'set code [catch {{rb::hook::stage_end cts "{hook}"}} msg opts]\n'
        'puts "CODE $code STAGE $rb_stage"\nputs "MSG $msg"\n'
        'puts "INFO [dict get $opts -errorinfo]"\n',
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "CODE 1 STAGE cts" in out
    assert f"MSG Stage cts: Hook stage-end ({hook}): boom" in out
    info = out[out.index("INFO ") + len("INFO ") :]
    assert info.startswith(f"Stage cts: Hook stage-end ({hook}): boom\n")
    assert f'(file "{hook}" line 1)' in info


# Extraction and the final reports


@pytest.mark.parametrize("stages", [True, False], ids=["staged", "one-session"])
def test_every_spef_is_read_as_the_after_extraction_hooks_left_it(
    tmp_path, monkeypatch, stages
):
    hook = (
        'puts "SPEF $rb_spef"\nset fh [open $rb_spef a]\nputs $fh edited\nclose $fh\n'
    )
    hooks = _hooks(tmp_path, ("after-extraction",), hook)
    backend, artefacts = _run_backend(
        tmp_path,
        monkeypatch,
        hooks,
        stages=stages,
        checkpoints=None,
        post_route=_ROUNDS,
        corners=["ss", "ff"],
        env=_ROUNDS_ENV,
        spef_stubs=True,
    )

    res = backend.run()

    assert isinstance(res, PnrPassResults), res.results
    (run_dir,) = _runs(artefacts)
    out_dir = artefacts
    rounds = [e["round"] for e in _events(run_dir) if e["event"] == "post_route_round"]
    assert rounds == [0, 1, 2]
    for round_ in rounds:
        spef = run_dir / f"06_post_route_repair.r{round_}.spef"
        log = (run_dir / f"06_post_route_repair.r{round_}.extract.log").read_text()
        assert f"SPEF {spef}" in log
        for corner in ("ss", "ff"):
            assert (
                f"stub: read_spef -corner {corner} {spef.name}: "
                f"spef r{round_}\nedited" in log
            )
        if round_:
            repair = (
                run_dir / f"06_post_route_repair.r{round_}.repair.log"
            ).read_text()
            for corner in ("ss", "ff"):
                assert (
                    f"stub: read_spef -corner {corner} "
                    f"06_post_route_repair.r{round_ - 1}.spef: "
                    f"spef r{round_ - 1}\nedited" in repair
                )
    log = (artefacts / "pnr.log").read_text()
    final = log[log.rindex(">>> Final reports") :]
    assert f"SPEF {out_dir}/demo_top.routed.spef" in log
    for corner in ("ss", "ff"):
        assert (
            f"stub: read_spef -corner {corner} demo_top.routed.spef: spef final\nedited"
            in final
        )


def test_the_after_extraction_hooks_need_rcx_rules(tmp_path, monkeypatch):
    hooks = _hooks(tmp_path, ("after-extraction",))
    cfg = replace(_pnr_cfg(tmp_path, None), hooks=hooks)
    backend, _artefacts = _backend(tmp_path, monkeypatch, pnr_cfg=cfg)
    logged = _logged(monkeypatch)

    res = backend.run()

    assert isinstance(res, PnrFailResults)
    assert res.results["fail_stage"] == "setup"
    assert res.results["desc"].startswith(
        "hooks.after-extraction needs rcx-rules on PDK 'nangate45'"
    )
    assert [e for _l, e, _f in logged if e.endswith("_without_rcx")] == [
        "pnr.after_extraction_without_rcx"
    ]


def test_a_probe_without_rcx_rules_may_set_after_extraction(tmp_path, monkeypatch):
    """A probe stops after global route, where nothing is extracted."""
    hooks = _hooks(tmp_path, ("after-extraction",))
    cfg = replace(_pnr_cfg(tmp_path, None), hooks=hooks, mode=PnrMode.PROBE)
    backend, _artefacts = _backend(tmp_path, monkeypatch, pnr_cfg=cfg)

    assert isinstance(backend.run(), PnrPassResults)


def test_after_reports_hooks_run_on_the_final_parasitics_before_the_outputs(
    tmp_path, monkeypatch
):
    hooks = _hooks(tmp_path, ("after-reports",))
    backend, artefacts = _run_backend(
        tmp_path, monkeypatch, hooks, checkpoints=None, spef_stubs=True
    )

    assert isinstance(backend.run(), PnrPassResults)
    log = (artefacts / "pnr.log").read_text().splitlines()
    read = log.index("stub: read_spef  demo_top.routed.spef: spef final")
    assert log.index(">>> Final reports") < read < log.index("MARK after-reports")
    assert log.index("MARK after-reports") < log.index("stub: write_def demo_top.def")


def test_a_failing_after_reports_hook_fails_the_run(tmp_path, monkeypatch):
    hooks = _hooks(tmp_path, ("after-reports",), 'error "summary failed"\n')
    backend, artefacts = _run_backend(tmp_path, monkeypatch, hooks, checkpoints=None)

    res = backend.run()

    assert isinstance(res, PnrFailResults)
    assert "Hook after-reports" in res.results["desc"]
    assert not (artefacts / "demo_top.routed.odb").exists()


# Commands a hook leaves behind


def test_the_command_tracker_covers_the_new_points(tmp_path, monkeypatch, caplog):
    hooks = {
        "before-cts": _hooks(
            tmp_path, ("before-cts",), "rename check_placement rb_test_check\n"
        )["before-cts"],
        "stage-end": _hooks(
            tmp_path,
            ("stage-end",),
            'if {$rb_stage eq "place"} { proc rb_test_stage_end {} {} }\n',
        )["stage-end"],
    }
    backend, _artefacts = _run_backend(tmp_path, monkeypatch, hooks, stages=True)

    with caplog.at_level("INFO"):
        assert isinstance(backend.run(), PnrPassResults)

    def _logged_fields(event):
        return [
            r.rtl_fields
            for r in caplog.records
            if getattr(r, "rtl_event", None) == event
        ]

    assert _logged_fields("pnr.hook_commands_not_kept") == [
        {
            "pnr": "demo_pnr",
            "point": "before-cts",
            "path": hooks["before-cts"][0],
            "commands": ["check_placement"],
        }
    ]
    added = {
        (f["point"], tuple(f["commands"]))
        for f in _logged_fields("pnr.hook_commands_added")
    }
    assert added == {
        ("before-cts", ("rb_test_check",)),
        ("stage-end", ("rb_test_stage_end",)),
    }


# After timing repair

#: Prints the repair that just ran and, in a post-route round, its number.
_TIMING_REPAIR_HOOK = (
    'puts "MARK after-timing-repair $rb_repair '
    '[expr {[info exists rb_repair_round] ? $rb_repair_round : "-"}]"\n'
)

#: The marks of the hooks around the timing repairs in a run whose two post-route rounds both repair.
_TIMING_REPAIR_MARKS = [
    "MARK after-cts",
    "MARK after-timing-repair post_cts -",
    "MARK after-cts-repair",
    "MARK after-global-route",
    "MARK after-timing-repair global_route -",
    "MARK after-global-route-repair",
    "MARK before-detailed-route",
    "MARK after-timing-repair post_route 1",
    "MARK before-detailed-route",
    "MARK after-timing-repair post_route 2",
    "MARK before-detailed-route",
]


def _timing_repair_hooks(tmp_path) -> dict:
    hooks = _hooks(
        tmp_path,
        (
            "after-cts",
            "after-cts-repair",
            "after-global-route",
            "after-global-route-repair",
            "before-detailed-route",
        ),
    )
    return {
        **hooks,
        **_hooks(tmp_path, ("after-timing-repair",), _TIMING_REPAIR_HOOK),
    }


def _between(text: str, *anchors: str) -> None:
    """Assert that `anchors` start in `text` in order, each after the one before."""
    at = 0
    for anchor in anchors:
        at = text.index(anchor, at) + 1


@pytest.mark.parametrize("reroute", ["eco", "full"])
@pytest.mark.parametrize("stages", [True, False], ids=["staged", "one-session"])
def test_after_timing_repair_runs_after_each_timing_repair_before_legalizing(
    tmp_path, monkeypatch, stages, reroute
):
    backend, artefacts = _run_backend(
        tmp_path,
        monkeypatch,
        _timing_repair_hooks(tmp_path),
        stages=stages,
        post_route={
            "repair-design": {"after-timing": True},
            "setup-margin": 0.1,
            "hold-margin": 0.05,
            "rounds": 2,
            "reroute": reroute,
        },
        env={"RB_STUB_SETUP": "-0.2 -0.1 0.05"},
    )

    res = backend.run()

    assert isinstance(res, PnrPassResults), res.results
    log = (artefacts / "pnr.log").read_text().splitlines()
    assert [line for line in log if line.startswith("MARK ")] == _TIMING_REPAIR_MARKS
    (run_dir,) = _runs(artefacts)
    if stages:
        cts = (run_dir / "03_cts.tcl").read_text()
        gr = (run_dir / "04_global_route.tcl").read_text()
    else:
        cts = gr = (artefacts / "pnr.tcl").read_text()
    run = "rb::hook::run after-timing-repair "
    _between(
        cts,
        'puts ">>> Post-CTS timing repair"',
        "\nrepair_timing -hold\n",
        "set rb_repair post_cts\n",
        run,
        ">>> Detail placement (legalize repair cells)",
        "rb::hook::run after-cts-repair ",
    )
    _between(
        gr,
        ">>> Global-route repair",
        "\nrepair_timing -hold",
        "\nset rb_repair global_route\n",
        run,
        "\nglobal_route -start_incremental\n",
        "\ndetailed_placement",
        "rb::hook::run after-global-route-repair ",
    )
    for round_ in (1, 2):
        text = (run_dir / f"06_post_route_repair.r{round_}.repair.tcl").read_text()
        steps = [
            "\nrepair_timing -hold",
            f"\nset rb_repair post_route\nset rb_repair_round {round_}\n",
            run,
            "after timing repair",
            "\nrepair_design",
        ]
        if reroute == "eco":
            # Inside the ECO's repairs and incremental global route.
            steps = [
                "\nglobal_route -start_incremental",
                f"\nrb::eco::repair {round_} {{\n",
                *steps,
                "\n}\n",
            ]
        _between(
            text,
            *steps,
            "\ndetailed_placement",
            "\nrb::global_connect\n",
            "rb::hook::run before-detailed-route ",
        )
        assert text.count(run) == 1


def test_after_timing_repair_runs_only_after_a_timing_repair(tmp_path, monkeypatch):
    """Not where a repair is skipped or runs no `repair_timing`: a probe, a `repair_design`-only global-route repair, the electrical round."""
    hooks = _hooks(tmp_path, ("after-timing-repair",), _TIMING_REPAIR_HOOK)
    for name in ("probe", "closure"):
        (tmp_path / name).mkdir()
    probe, artefacts = _run_backend(
        tmp_path / "probe", monkeypatch, hooks, mode=PnrMode.PROBE
    )
    assert isinstance(probe.run(), PnrPassResults)
    assert "MARK after-timing-repair" not in (artefacts / "pnr.log").read_text()

    backend, artefacts = _run_backend(
        tmp_path / "closure",
        monkeypatch,
        hooks,
        post_route={**_ROUNDS, "rounds": 1, "electrical-repair": {"slew-margin": 10}},
        env={"RB_STUB_SETUP": "-0.2 0.05", "RB_STUB_SLEW": "3 2 1"},
        platform={"global_route_repair": {"repair-design": {}}},
    )
    assert isinstance(backend.run(), PnrPassResults)
    marks = [
        line
        for line in (artefacts / "pnr.log").read_text().splitlines()
        if line.startswith("MARK ")
    ]
    assert marks == [
        "MARK after-timing-repair post_cts -",
        "MARK after-timing-repair post_route 1",
    ]
    (run_dir,) = _runs(artefacts)
    electrical = (run_dir / "06_post_route_repair.r2.repair.tcl").read_text()
    assert "after-timing-repair" not in electrical


@pytest.mark.parametrize("stage", ["global_route", "detailed_route"])
def test_a_resume_after_cts_refuses_an_edited_after_timing_repair_hook(
    tmp_path, monkeypatch, stage
):
    hooks = _hooks(tmp_path, ("after-timing-repair",), _TIMING_REPAIR_HOOK)
    backend, artefacts = _run_backend(
        tmp_path, monkeypatch, hooks, stages=True, stop_after="global_route"
    )
    assert isinstance(backend.run(), PnrPassResults)
    _write(Path(hooks["after-timing-repair"][0]), 'puts "edited"\n')

    backend, _ = _run_backend(
        tmp_path, monkeypatch, hooks, stages=True, resume="latest", resume_from=stage
    )
    res = backend.run()

    assert isinstance(res, PnrFailResults)
    assert res.results["fail_stage"] == "resume"
    assert "hook_after_timing_repair" in res.results["desc"]

    backend, _ = _run_backend(
        tmp_path, monkeypatch, hooks, stages=True, resume="latest", resume_from="cts"
    )
    assert isinstance(backend.run(), PnrPassResults)


@pytest.mark.parametrize("stages", [True, False], ids=["staged", "one-session"])
def test_rb_repair_exists_only_for_the_after_timing_repair_hooks(
    tmp_path, monkeypatch, stages
):
    points = (
        "after-timing-repair",
        "after-cts-repair",
        "after-global-route",
        "after-global-route-repair",
        "before-detailed-route",
        "stage-end",
    )
    hooks = {
        point: _hooks(
            tmp_path,
            (point,),
            f'puts "SEES {point} [info exists rb_repair] '
            '[info exists rb_repair_round]"\n',
        )[point]
        for point in points
    }
    backend, artefacts = _run_backend(
        tmp_path,
        monkeypatch,
        hooks,
        stages=stages,
        post_route={**_ROUNDS, "rounds": 1},
        env={"RB_STUB_SETUP": "-0.2 0.05"},
    )

    assert isinstance(backend.run(), PnrPassResults)
    seen = [
        line
        for line in (artefacts / "pnr.log").read_text().splitlines()
        if line.startswith("SEES ")
    ]
    assert seen.count("SEES after-timing-repair 1 0") == 2
    assert seen.count("SEES after-timing-repair 1 1") == 1
    others = [line for line in seen if "after-timing-repair" not in line]
    assert others and all(line.endswith(" 0 0") for line in others)
