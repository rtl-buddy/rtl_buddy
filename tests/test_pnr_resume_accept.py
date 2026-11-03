"""Tests for `rb pnr --resume --accept-change`: differences from a fresh run that a resume accepts by name, and how the run records them.

Runs use the stub OpenROAD harness of `test_pnr_checkpoints` through the staged-run helpers of `test_pnr_stages`.
"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_pnr_checkpoints import _backend, _platform, _pnr_cfg, _runs
from test_pnr_stages import _only_run, _session_hook, _snapshot, _staged

from rtl_buddy.config.pdk import PlacementFile
from rtl_buddy.tools import pnr_abstract, pnr_stages


def _hooked_cfg(tmp_path, **changes):
    """The staged run with a session hook; the hook and the SDC keep their content when they exist."""
    hook = tmp_path / "hooks" / "session.tcl"
    sdc = tmp_path / "constraints.sdc"
    kept = sdc.read_text() if sdc.exists() else None
    cfg = _pnr_cfg(tmp_path, stages=True)
    if kept is not None:
        sdc.write_text(kept)
    if not hook.exists():
        _session_hook(tmp_path)
    return replace(cfg, hooks={"session": (str(hook),)}, **changes)


def _first_run(tmp_path, monkeypatch, cfg=None, stop_after="global_route") -> Path:
    backend, artefacts = _staged(
        tmp_path,
        monkeypatch,
        pnr_cfg=cfg or _hooked_cfg(tmp_path),
        stop_after=stop_after,
    )
    assert backend.run().results["result"] == "PASS"
    return _only_run(artefacts)


def _edit_hook(tmp_path, text='puts "MARK session edited"\n') -> Path:
    hook = tmp_path / "hooks" / "session.tcl"
    hook.write_text(text)
    return hook


def _diamond(backend):
    """Point `backend` at a platform whose `placement.rotated-macro-footprint` is `diamond-legalizer`: a session hook may rotate a macro, so place and every later script change."""
    backend.root_cfg.get_pnr_platform_cfg.return_value = _platform(
        backend.root_cfg.get_pnr_platform_cfg.return_value.get_pdk(),
        placement=PlacementFile(rotated_macro_footprint="diamond-legalizer"),
    )
    return backend


def _resume(tmp_path, monkeypatch, run, from_stage="global_route", platform=None, **kw):
    """A resume of `run` invoked from `tmp_path`, the directory of the run's config."""
    kw.setdefault("pnr_cfg", _hooked_cfg(tmp_path))
    kw.setdefault("invocation_dir", str(tmp_path))
    backend, artefacts = _backend(
        tmp_path, monkeypatch, resume=run, resume_from=from_stage, **kw
    )
    if platform is not None:
        platform(backend)
    return backend, artefacts


def _refused(backend, artefacts) -> str:
    before = _snapshot(artefacts)
    res = backend.run()
    assert res.results["fail_stage"] == "resume", res.results
    assert _snapshot(artefacts) == before
    return res.results["desc"]


def _manifest(run_dir: Path) -> dict:
    return json.loads((run_dir / "manifest.json").read_text())


def _logged(caplog, event):
    return [
        (r.levelname, r.rtl_fields)
        for r in caplog.records
        if getattr(r, "rtl_event", None) == event
    ]


@pytest.mark.parametrize(
    "spelling",
    [
        lambda tmp_path: "hooks/session.tcl",
        lambda tmp_path: "./hooks/../hooks/session.tcl",
        lambda tmp_path: str(tmp_path / "hooks" / "session.tcl"),
    ],
    ids=["relative", "unnormalised", "absolute"],
)
def test_an_accepted_session_hook_edit_resumes_and_is_recorded(
    tmp_path, monkeypatch, caplog, spelling
):
    first = _first_run(tmp_path, monkeypatch)
    old = (tmp_path / "hooks" / "session.tcl").read_bytes()
    hook = _edit_hook(tmp_path)

    desc = _refused(*_resume(tmp_path, monkeypatch, first.name))
    assert "input hook_session differs" in desc
    assert desc.endswith("(--accept-change hooks/session.tcl)")

    backend, artefacts = _resume(
        tmp_path, monkeypatch, first.name, accept_changes=(spelling(tmp_path),)
    )
    with caplog.at_level("INFO"):
        res = backend.run()

    assert res.results["result"] == "PASS", res.results
    second = next(r for r in _runs(artefacts) if r != first)
    assert "MARK session edited" in (second / "04_global_route.log").read_text()
    manifest = _manifest(second)
    (record,) = manifest["accepted_changes"]
    (path,) = [f["path"] for f in manifest["inputs"]["hook_session"]]
    assert record == {
        "change": "hooks/session.tcl",
        "role": "hook_session",
        "old": {"path": path, "sha256": hashlib.sha256(old).hexdigest()},
        "new": {"path": path, "sha256": hashlib.sha256(hook.read_bytes()).hexdigest()},
    }
    (accepted,) = res.results["accepted_changes"]
    assert accepted["change"] == "hooks/session.tcl"
    assert accepted["new"]["path"] == str(hook)
    ((level, fields),) = _logged(caplog, "pnr.resume_accepted_change")
    assert level == "WARNING"
    assert fields["change"] == "hooks/session.tcl"
    assert fields["role"] == "hook_session"


def test_one_value_accepts_a_setting_that_changes_every_inherited_script(
    tmp_path, monkeypatch
):
    first = _first_run(tmp_path, monkeypatch)

    desc = _refused(*_resume(tmp_path, monkeypatch, first.name, platform=_diamond))
    assert "02_place.tcl" in desc and "(--accept-change script:place)" in desc
    assert "03_cts.tcl" in desc and "(--accept-change script:cts)" in desc
    assert desc.endswith("--accept-change script accepts every stage script")

    backend, artefacts = _resume(
        tmp_path, monkeypatch, first.name, platform=_diamond, accept_changes=("script",)
    )
    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    second = next(r for r in _runs(artefacts) if r != first)
    records = _manifest(second)["accepted_changes"]
    assert [(r["change"], r["stage"], r["script"]) for r in records] == [
        ("script:place", "place", "02_place.tcl"),
        ("script:cts", "cts", "03_cts.tcl"),
    ]
    for r in records:
        assert r["first_difference"].startswith("line ")
        assert r["old_identity"] != r["new_identity"]
    assert "-use_diamond_legalizer" in (second / "04_global_route.tcl").read_text()


def test_only_the_named_script_is_accepted(tmp_path, monkeypatch):
    first = _first_run(tmp_path, monkeypatch)

    desc = _refused(
        *_resume(
            tmp_path,
            monkeypatch,
            first.name,
            platform=_diamond,
            accept_changes=("script:place",),
        )
    )

    assert "02_place.tcl" not in desc
    assert "(--accept-change script:cts)" in desc
    assert "accepts every stage script" not in desc


def test_a_refusal_lists_every_script_and_input_difference(tmp_path, monkeypatch):
    first = _first_run(tmp_path, monkeypatch)
    _edit_hook(tmp_path)
    sdc = tmp_path / "constraints.sdc"
    original = sdc.read_text()
    cfg = _hooked_cfg(tmp_path)
    cfg = replace(cfg, floorplan=replace(cfg.floorplan, utilization=0.6))
    sdc.write_text(original + "set_input_delay 0.1 [all_inputs]\n")

    desc = _refused(*_resume(tmp_path, monkeypatch, first.name, pnr_cfg=cfg))

    floorplan, sdc_reason, hook = desc.split("; ")
    assert "01_floorplan.tcl" in floorplan
    assert floorplan.endswith("(--accept-change script:floorplan)")
    # Only the floorplan session reads the SDC; later ones read its checkpoint copy.
    assert sdc_reason.startswith("input sdc differs")
    assert sdc_reason.endswith(
        "but only the stages before --from global_route read it, so run without --resume"
    )
    assert hook.endswith("(--accept-change hooks/session.tcl)")

    sdc.write_text(original)
    backend, _ = _resume(
        tmp_path,
        monkeypatch,
        first.name,
        pnr_cfg=cfg,
        accept_changes=("script:floorplan", "hooks/session.tcl"),
    )
    assert backend.run().results["result"] == "PASS"


@pytest.mark.parametrize(
    "point, from_stage, accepted",
    [
        ("after-cts", "global_route", False),
        ("after-timing-repair", "global_route", True),
        ("after-timing-repair", "detailed_route", False),
        ("stage-end", "detailed_route", True),
    ],
)
def test_a_hook_is_accepted_only_when_a_resumed_stage_runs_it(
    tmp_path, monkeypatch, point, from_stage, accepted
):
    hook = tmp_path / "hooks" / f"{point}.tcl"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text('puts "MARK hook"\n')
    cfg = replace(_pnr_cfg(tmp_path, stages=True), hooks={point: (str(hook),)})
    first = _first_run(tmp_path, monkeypatch, cfg=cfg, stop_after="detailed_route")
    hook.write_text('puts "MARK hook edited"\n')
    value = f"hooks/{point}.tcl"

    backend, artefacts = _resume(
        tmp_path,
        monkeypatch,
        first.name,
        from_stage=from_stage,
        pnr_cfg=cfg,
        accept_changes=(value,),
    )

    if accepted:
        res = backend.run()
        assert res.results["result"] == "PASS", res.results
        assert [c["change"] for c in res.results["accepted_changes"]] == [value]
    else:
        desc = _refused(backend, artefacts)
        assert f"--accept-change {value} names a difference no value accepts" in desc
        assert f"only the stages before --from {from_stage} read it" in desc
        assert "(--accept-change" not in desc


def test_an_unreadable_input_is_never_accepted(tmp_path, monkeypatch):
    first = _first_run(tmp_path, monkeypatch)
    hook = _edit_hook(tmp_path)
    hook.chmod(0)
    try:
        desc = _refused(
            *_resume(
                tmp_path,
                monkeypatch,
                first.name,
                accept_changes=("hooks/session.tcl",),
            )
        )
    finally:
        hook.chmod(0o644)

    assert "names a difference no value accepts" in desc
    assert f"input hook_session {hook} is missing or unreadable" in desc
    assert "(--accept-change" not in desc


@pytest.mark.parametrize(
    "invoked_from, printed",
    [
        ("", "hooks/session.tcl"),
        ("hooks", "session.tcl"),
        ("elsewhere", None),
    ],
)
def test_input_values_are_relative_to_the_invocation_directory(
    tmp_path, monkeypatch, invoked_from, printed
):
    first = _first_run(tmp_path, monkeypatch)
    hook = _edit_hook(tmp_path)
    where = tmp_path / invoked_from
    where.mkdir(exist_ok=True)
    printed = printed or str(hook)

    desc = _refused(
        *_resume(tmp_path, monkeypatch, first.name, invocation_dir=str(where))
    )
    assert desc.endswith(f"(--accept-change {printed})")

    backend, _ = _resume(
        tmp_path,
        monkeypatch,
        first.name,
        invocation_dir=str(where),
        accept_changes=(printed,),
    )
    assert backend.run().results["result"] == "PASS"


def test_an_input_outside_the_invocation_directory_is_named_by_its_absolute_path(
    tmp_path, monkeypatch
):
    outside = tmp_path / "shared" / "session.tcl"
    outside.parent.mkdir()
    outside.write_text('puts "MARK session"\n')
    project = tmp_path / "project"
    project.mkdir()
    cfg = replace(_hooked_cfg(project), hooks={"session": (str(outside),)})
    first = _first_run(project, monkeypatch, cfg=cfg)
    outside.write_text('puts "MARK session edited"\n')

    desc = _refused(*_resume(project, monkeypatch, first.name, pnr_cfg=cfg))
    assert desc.endswith(f"(--accept-change {outside})")

    backend, _ = _resume(
        project,
        monkeypatch,
        first.name,
        pnr_cfg=cfg,
        accept_changes=("../shared/session.tcl",),
    )
    assert backend.run().results["result"] == "PASS"


def _probe_manifest(first: Path) -> None:
    manifest = _manifest(first)
    manifest["mode"] = "probe"
    (first / "manifest.json").write_text(json.dumps(manifest))


@pytest.mark.parametrize(
    "spoil, options, expected",
    [
        (
            lambda first, tmp_path: None,
            {"openroad": "26Q4-other"},
            "OpenROAD is 26Q4-other",
        ),
        (lambda first, tmp_path: _probe_manifest(first), {}, "is a probe"),
        (
            lambda first, tmp_path: (first / "03_cts.odb").write_text("edited\n"),
            {},
            "03_cts.odb changed since the run wrote it",
        ),
        (
            lambda first, tmp_path: (first / "03_cts.odb").unlink(),
            {},
            "checkpoint file odb of stage cts is missing",
        ),
        (
            lambda first, tmp_path: None,
            {"from_stage": "finish"},
            "did not finish with its checkpoint written",
        ),
        (
            lambda first, tmp_path: (first / "manifest.json").write_text("{"),
            {},
            "has no readable manifest.json",
        ),
        (lambda first, tmp_path: None, {"drop": True}, "is no longer configured"),
        (lambda first, tmp_path: None, {"add": True}, "is newly configured"),
    ],
    ids=[
        "openroad",
        "probe",
        "changed-checkpoint",
        "missing-checkpoint",
        "unfinished",
        "manifest",
        "dropped-input",
        "added-input",
    ],
)
def test_what_no_value_accepts(tmp_path, monkeypatch, spoil, options, expected):
    first_cfg = _pnr_cfg(tmp_path, stages=True) if options.get("add") else None
    first = _first_run(tmp_path, monkeypatch, cfg=first_cfg)
    spoil(first, tmp_path)
    kw = {"pnr_cfg": _pnr_cfg(tmp_path, stages=True)} if options.get("drop") else {}

    backend, artefacts = _resume(
        tmp_path,
        monkeypatch,
        first.name,
        from_stage=options.get("from_stage", "global_route"),
        accept_changes=("script", "hooks/session.tcl"),
        **kw,
    )
    if "openroad" in options:
        monkeypatch.setattr(
            backend, "_probe_openroad_version", lambda: options["openroad"]
        )

    assert expected in _refused(backend, artefacts)


def test_a_value_that_names_nothing_refuses(tmp_path, monkeypatch):
    first = _first_run(tmp_path, monkeypatch)

    desc = _refused(
        *_resume(tmp_path, monkeypatch, first.name, accept_changes=("hooks/typo.tcl",))
    )
    assert "--accept-change hooks/typo.tcl names no difference" in desc
    assert desc.endswith("no inherited script or input differs")

    _edit_hook(tmp_path)
    desc = _refused(
        *_resume(
            tmp_path,
            monkeypatch,
            first.name,
            accept_changes=("hooks/session.tcl", "script"),
        )
    )
    assert "--accept-change script names no difference" in desc
    assert "input hook_session differs" in desc
    assert "(--accept-change hooks/session.tcl)" in desc


def test_a_resume_without_the_flag_records_nothing(tmp_path, monkeypatch):
    first = _first_run(tmp_path, monkeypatch)

    backend, artefacts = _resume(tmp_path, monkeypatch, first.name)
    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert "accepted_changes" not in res.results
    second = next(r for r in _runs(artefacts) if r != first)
    assert "accepted_changes" not in _manifest(second)


def test_a_resume_of_a_resume_keeps_its_accepted_changes(tmp_path, monkeypatch, caplog):
    first = _first_run(tmp_path, monkeypatch)
    _edit_hook(tmp_path)
    backend, artefacts = _resume(
        tmp_path,
        monkeypatch,
        first.name,
        platform=_diamond,
        accept_changes=("script", "hooks/session.tcl"),
        stop_after="detailed_route",
    )
    assert backend.run().results["result"] == "PASS"
    second = next(r for r in _runs(artefacts) if r != first)
    accepted = _manifest(second)["accepted_changes"]
    assert [r["change"] for r in accepted] == [
        "script:place",
        "script:cts",
        "hooks/session.tcl",
    ]

    # The scripts and the hook are as `second` accepted them: no value needed.
    backend, _ = _resume(
        tmp_path, monkeypatch, second.name, from_stage="finish", platform=_diamond
    )
    caplog.clear()
    with caplog.at_level("INFO"):
        res = backend.run()
    assert res.results["result"] == "PASS", res.results
    third = next(r for r in _runs(artefacts) if r not in (first, second))
    carried = _manifest(third)["accepted_changes"]
    assert carried == [{**r, "inherited_from": second.name} for r in accepted]
    assert [
        f["inherited_from"] for _l, f in _logged(caplog, "pnr.resume_accepted_change")
    ] == [second.name] * 3

    # Values that name a carried change are not typos.
    backend, _ = _resume(
        tmp_path,
        monkeypatch,
        second.name,
        from_stage="finish",
        platform=_diamond,
        accept_changes=("script", "hooks/session.tcl"),
    )
    assert backend.run().results["result"] == "PASS"

    # A resume of `third` names the run that accepted each change.
    backend, _ = _resume(
        tmp_path, monkeypatch, third.name, from_stage="finish", platform=_diamond
    )
    res = backend.run()
    assert res.results["result"] == "PASS", res.results
    assert {r["inherited_from"] for r in res.results["accepted_changes"]} == {
        second.name
    }

    # Away from the accepted state, the normal check applies.
    desc = _refused(*_resume(tmp_path, monkeypatch, second.name, from_stage="finish"))
    # `first` ran place and cts as they render now; `second` ran the later stages with the setting.
    assert "script:place" not in desc and "script:cts" not in desc
    assert "(--accept-change script:global_route)" in desc
    assert "(--accept-change script:detailed_route)" in desc


@pytest.mark.parametrize(
    "hook_now",
    ['puts "MARK session"\n', 'puts "MARK session edited again"\n'],
    ids=["reverted", "edited-again"],
)
def test_an_input_change_is_carried_only_while_the_file_is_as_accepted(
    tmp_path, monkeypatch, hook_now
):
    first = _first_run(tmp_path, monkeypatch)
    hook = _edit_hook(tmp_path)
    accepted_digest = hashlib.sha256(hook.read_bytes()).hexdigest()
    backend, artefacts = _resume(
        tmp_path,
        monkeypatch,
        first.name,
        accept_changes=("hooks/session.tcl",),
        stop_after="detailed_route",
    )
    assert backend.run().results["result"] == "PASS"
    second = next(r for r in _runs(artefacts) if r != first)
    hook.write_text(hook_now)

    desc = _refused(*_resume(tmp_path, monkeypatch, second.name, from_stage="finish"))
    assert "input hook_session differs" in desc
    assert desc.endswith("(--accept-change hooks/session.tcl)")

    backend, _ = _resume(
        tmp_path,
        monkeypatch,
        second.name,
        from_stage="finish",
        accept_changes=("hooks/session.tcl",),
    )
    res = backend.run()
    assert res.results["result"] == "PASS", res.results
    # Only this run's record: `second`'s no longer holds.
    (record,) = res.results["accepted_changes"]
    assert "inherited_from" not in record
    assert record["old"]["sha256"] == accepted_digest
    assert record["new"]["sha256"] == hashlib.sha256(hook_now.encode()).hexdigest()


def test_the_abstract_records_accepted_changes_without_going_stale(tmp_path):
    from unittest.mock import MagicMock

    from test_pnr_blocks import _block_platform, _block_suite, _ref

    from rtl_buddy.tools.pnr_abstract import resolve_block

    suite = _block_suite(tmp_path)
    abstract = tmp_path / "blk/artefacts/blk_pnr/abstract"
    old = json.loads((abstract / "abstract.manifest.json").read_text())
    sdc = tmp_path / "blk/blk.sdc"
    change = {
        "change": "blk.sdc",
        "role": "sdc",
        "old": {"path": str(sdc), "sha256": "0" * 64},
        "new": {"path": str(sdc), "sha256": "1" * 64},
        "inherited_from": "20260101T000000-1",
    }
    pnr_abstract.write_manifest(
        str(abstract),
        artefact_dir=str(abstract.parent),
        design="blk_top",
        run="blk_pnr",
        platform="p",
        pdk="p",
        openroad={},
        technology={},
        inputs={"sdc": str(sdc)},
        config={k: v for k, v in old["config"].items() if k != "sha256"},
        accepted_changes=[change],
    )

    written = json.loads((abstract / "abstract.manifest.json").read_text())
    (recorded,) = written["accepted_changes"]
    assert recorded["old"]["path"] == written["inputs"]["sdc"]["path"]
    assert not recorded["new"]["path"].startswith("/")
    assert recorded["inherited_from"] == "20260101T000000-1"
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = _block_platform(tmp_path)
    assert pnr_abstract.block_changes(resolve_block(_ref(suite)), root_cfg) == []


def test_accept_change_needs_resume():
    assert pnr_stages.option_error(None, None, None, ("script",)) == (
        "--accept-change needs --resume"
    )
    assert pnr_stages.option_error(None, "latest", "cts", ("script",)) is None


@pytest.mark.parametrize(
    "name, expected",
    [
        ("a", "--accept-change needs --resume"),
        (None, "--accept-change need a single named pnr run"),
    ],
)
def test_a_lone_accept_change_fails_before_any_run(
    tmp_path, monkeypatch, name, expected
):
    from test_pnr_plan import _run, _suite
    from test_pnr_stages import _stage_driver

    from rtl_buddy.errors import FatalRtlBuddyError

    suite = _suite(tmp_path / "pnr.yaml", _run("a"))
    rb, seen = _stage_driver(monkeypatch)

    with pytest.raises(FatalRtlBuddyError, match=expected):
        rb._do_pnr_suite(
            suite, pnr_name=name, run_synth=True, accept_changes=("script",)
        )
    assert seen == {}


def test_rb_pnr_passes_each_accept_change_value(minimal_project, monkeypatch):
    from typer.testing import CliRunner

    from rtl_buddy.rtl_buddy import RtlBuddy

    (minimal_project / "pnr.yaml").write_text(
        "rtl-buddy-filetype: pnr_config\nruns: []\n"
    )
    seen = {}

    def _capture(self, suite, **kwargs):
        seen.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(RtlBuddy, "_do_pnr_suite", _capture)
    rb = RtlBuddy(name="test_pnr_resume_accept")
    try:
        result = CliRunner().invoke(
            rb.app,
            [
                "pnr",
                "r",
                "-c",
                "pnr.yaml",
                "--resume",
                "latest",
                "--from",
                "cts",
                "--accept-change",
                "script",
                "--accept-change",
                "hooks/session.tcl",
            ],
        )
    finally:
        rb._artifact_locks.release_all()

    assert seen.get("accept_changes") == ("script", "hooks/session.tcl"), result.output
    assert seen["invocation_dir"] == str(minimal_project.resolve())


def test_result_rows_carry_the_accepted_changes():
    from rtl_buddy.rtl_buddy import RtlBuddy
    from rtl_buddy.runner.pnr_results import PnrPassResults

    change = {"change": "script:cts", "stage": "cts"}
    res = PnrPassResults(name="r/results", fields={"accepted_changes": [change]})

    row = RtlBuddy._pnr_result_row(None, {"pnr_name": "r", "results": res})

    assert row["accepted_changes"] == [change]


def test_accepted_change_messages_read_plainly():
    from rtl_buddy.logging_utils import _human_message

    script = _human_message(
        "pnr.resume_accepted_change",
        {
            "pnr": "p",
            "change": "script:cts",
            "stage": "cts",
            "first_difference": "line 5: was 'a', now 'b'",
        },
    )
    assert script == (
        'P&R "p": resumed with an accepted change (--accept-change script:cts): '
        "stage cts script, line 5: was 'a', now 'b'"
    )
    hook = _human_message(
        "pnr.resume_accepted_change",
        {
            "pnr": "p",
            "change": "hooks/s.tcl",
            "role": "hook_session",
            "old": {"path": "/x/hooks/s.tcl", "sha256": "a" * 64},
            "new": {"path": "/x/hooks/s.tcl", "sha256": "b" * 64},
            "inherited_from": "run1",
        },
    )
    assert hook.endswith(
        f"input hook_session /x/hooks/s.tcl, {'a' * 12} then, {'b' * 12} now "
        "(accepted by run run1)"
    )


@pytest.mark.parametrize("name", ["script", "script:cts"])
def test_an_input_named_like_a_script_value_is_named_with_a_leading_dot(tmp_path, name):
    path = tmp_path / name
    change = pnr_stages.input_change(str(path), str(tmp_path))
    assert change == f"./{name}"
    found = pnr_stages._Difference("", change, paths=frozenset({str(path)}))
    assert found.named_by(change, str(tmp_path))
    assert not found.named_by("script", str(tmp_path))
