"""Tests for the run-level `inputs:` of `rb pnr`, extra files the SDC or hooks source.

Pinned behaviour:

- `inputs` is a list of paths relative to `pnr.yaml`; a malformed value fails at load, and a missing file fails `rb pnr` before any run starts;
- each file is a checkpoint-manifest input read from the first stage, so `--resume` refuses when one changed or moved;
- each file is an abstract input, so editing one makes a hardened block stale, and deleting one is reported as stale, not as a config error;
- a run without `inputs` records no extra role and renders the flow as before.
"""

import json
from dataclasses import replace
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest
from test_pnr import _make_pdk_cfg, _make_pnr_cfg, _render_flow, _touch
from test_pnr import _platform as _flow_platform
from test_pnr_blocks import (
    _BLOCK_YAML,
    _block_platform,
    _block_suite,
    _sha,
    _synth_runner,
    _top_backend,
    _write,
)
from test_pnr_checkpoints import _backend, _pnr_cfg, _runs
from test_pnr_hooks import _stale_changes
from test_pnr_plan import _driver, _run, _suite

from rtl_buddy.config.pnr import PnrConfigFile, PnrSuiteConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_abstract, pnr_stages
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr


def _cfg_file(**kw):
    return PnrConfigFile(
        name="r", desc="r", synth="s", synth_path="synth.yaml", platform="p", **kw
    )


# Configuration


def test_inputs_default_to_none(tmp_path):
    assert _cfg_file().initialise(str(tmp_path)).get_inputs() == ()


@pytest.mark.parametrize("value", [None, []])
def test_unset_and_empty_inputs_are_none(tmp_path, value):
    assert _cfg_file(inputs=value).initialise(str(tmp_path)).get_inputs() == ()


def test_inputs_load_from_yaml_relative_to_the_file_in_order(tmp_path):
    _write(tmp_path / "window.tcl", "set a 1\n")
    _write(tmp_path / "scripts/closure.tcl", "set b 1\n")
    (tmp_path / "pnr.yaml").write_text(
        dedent("""\
        rtl-buddy-filetype: pnr_config
        runs:
          - name: r
            desc: d
            synth: s
            synth-path: synth.yaml
            platform: p
            inputs: [scripts/closure.tcl, window.tcl]
        """)
    )
    cfg = PnrSuiteConfig(str(tmp_path / "pnr.yaml")).get_runs("r")[0]
    assert cfg.get_inputs() == (
        str(tmp_path / "scripts/closure.tcl"),
        str(tmp_path / "window.tcl"),
    )


def test_a_missing_input_loads_and_fails_its_check_naming_it(tmp_path):
    _write(tmp_path / "here.tcl", "")
    cfg = _cfg_file(inputs=["here.tcl", "gone.tcl"]).initialise(str(tmp_path))
    with pytest.raises(FatalRtlBuddyError) as err:
        cfg.check_inputs()
    message = str(err.value)
    assert message.startswith("pnr run 'r': inputs names a file that does not exist")
    assert str(tmp_path / "gone.tcl") in message
    assert "here.tcl" not in message


def test_present_inputs_pass_their_check(tmp_path):
    _write(tmp_path / "here.tcl", "")
    _cfg_file(inputs=["here.tcl"]).initialise(str(tmp_path)).check_inputs()


def test_a_directory_is_not_an_input(tmp_path):
    (tmp_path / "dir").mkdir()
    with pytest.raises(FatalRtlBuddyError, match="does not exist"):
        _cfg_file(inputs=["dir"]).initialise(str(tmp_path)).check_inputs()


def _with_inputs(run_text: str, *names: str) -> str:
    return run_text + f"    inputs: [{', '.join(names)}]\n"


def test_rb_pnr_of_a_run_with_a_missing_input_fails_before_any_run(
    tmp_path, monkeypatch
):
    suite = _suite(tmp_path / "pnr.yaml", _with_inputs(_run("blk"), "gone.tcl"))
    rb, ran = _driver(monkeypatch, {})

    with pytest.raises(FatalRtlBuddyError) as err:
        rb._do_pnr_suite(suite, pnr_name="blk")

    assert str(tmp_path / "gone.tcl") in str(err.value)
    assert ran == []


def test_rb_pnr_checks_the_inputs_of_a_pulled_in_block(tmp_path, monkeypatch):
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("top", [("b", "blk", None)]),
        _with_inputs(_run("blk"), "gone.tcl"),
    )
    rb, ran = _driver(monkeypatch, {})

    with pytest.raises(FatalRtlBuddyError, match="gone.tcl"):
        rb._do_pnr_suite(suite)
    assert ran == []


def test_a_named_run_ignores_a_missing_input_of_another_run(tmp_path, monkeypatch):
    """A top in the same pnr.yaml as its block runs; the block's abstract is assessed by the top's run."""
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("top", [("b", "blk", None)]),
        _with_inputs(_run("blk"), "gone.tcl"),
    )
    rb, ran = _driver(monkeypatch, {})

    rb._do_pnr_suite(suite, pnr_name="top", accept_stale=True)

    assert ran == ["top"]


@pytest.mark.parametrize(
    "value",
    ["a.tcl", 3, True, [""], ["a.tcl", ""], ["a.tcl", 3], [["a.tcl"]], {"a": 1}],
)
def test_inputs_must_be_a_list_of_paths(tmp_path, value):
    _write(tmp_path / "a.tcl", "")
    with pytest.raises(FatalRtlBuddyError, match="pnr run 'r': inputs must be a list"):
        _cfg_file(inputs=value).initialise(str(tmp_path))


# Manifest inputs


def _backend_with(tmp_path, **cfg_fields):
    cfg = _make_pnr_cfg(tmp_path, **cfg_fields)
    cfg.resolve_synth_cfg = MagicMock(
        return_value=MagicMock(get_top=lambda: "t", get_name=lambda: "s")
    )
    return OpenRoadPnr("demo/openroad", cfg, str(tmp_path), MagicMock())


def test_inputs_are_checkpoint_and_abstract_inputs(tmp_path):
    platform = _flow_platform(_make_pdk_cfg(tmp_path), cts_buffer="BUF_X4")
    paths = (str(tmp_path / "window.tcl"), str(tmp_path / "closure.tcl"))
    _touch(*paths)
    script = str(tmp_path / "pnr.tcl")
    _touch(script)

    plain = _backend_with(tmp_path)
    backend = _backend_with(tmp_path, inputs=paths)

    assert "extra_inputs" not in plain._checkpoint_inputs(platform, script)
    assert "extra_inputs" not in plain.abstract_inputs(platform)

    manifest = backend._checkpoint_inputs(platform, script)
    assert [r["path"] for r in manifest["extra_inputs"]] == list(paths)
    assert all(r["sha256"] for r in manifest["extra_inputs"])
    assert backend.abstract_inputs(platform)["extra_inputs"] == list(paths)


def test_inputs_are_read_from_the_first_stage():
    for stage in pnr_stages.PNR_STAGES[1:]:
        assert pnr_stages.input_read_before("extra_inputs", stage)


def test_inputs_leave_the_rendered_flow_unchanged(tmp_path):
    platform = _flow_platform(_make_pdk_cfg(tmp_path), cts_buffer="BUF_X4")
    listed = tmp_path / "window.tcl"
    _touch(str(listed))

    plain = _render_flow(tmp_path, platform)
    listed_text = _render_flow(tmp_path, platform, inputs=(str(listed),))

    assert listed_text == plain
    assert str(listed) not in plain


# Resume


def _staged_cfg(tmp_path, helper: Path):
    return replace(_pnr_cfg(tmp_path, stages=True), inputs=(str(helper),))


def _stopped_run(tmp_path, monkeypatch, helper: Path) -> Path:
    backend, artefacts = _backend(
        tmp_path,
        monkeypatch,
        stages=True,
        stop_after="global_route",
        pnr_cfg=_staged_cfg(tmp_path, helper),
    )
    assert backend.run().results["result"] == "PASS"
    (run_dir,) = _runs(artefacts)
    return run_dir


def test_the_checkpoint_manifest_lists_the_inputs(tmp_path, monkeypatch):
    helper = _write(tmp_path / "window.tcl", "set a 1\n")
    first = _stopped_run(tmp_path, monkeypatch, helper)

    recorded = json.loads((first / "manifest.json").read_text())["inputs"]

    [entry] = recorded["extra_inputs"]
    assert entry["path"].endswith("window.tcl")
    assert entry["sha256"] == _sha("set a 1\n")


def test_resume_accepts_an_unchanged_input(tmp_path, monkeypatch):
    helper = _write(tmp_path / "window.tcl", "set a 1\n")
    first = _stopped_run(tmp_path, monkeypatch, helper)

    backend, _ = _backend(
        tmp_path,
        monkeypatch,
        stages=True,
        resume=first.name,
        resume_from="detailed_route",
        pnr_cfg=_staged_cfg(tmp_path, helper),
    )
    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["resumed_from"]["run_id"] == first.name


def test_resume_refuses_a_changed_input_naming_it(tmp_path, monkeypatch):
    helper = _write(tmp_path / "window.tcl", "set a 1\n")
    first = _stopped_run(tmp_path, monkeypatch, helper)
    helper.write_text("set a 2\n")

    backend, _ = _backend(
        tmp_path,
        monkeypatch,
        stages=True,
        resume=first.name,
        resume_from="detailed_route",
        pnr_cfg=_staged_cfg(tmp_path, helper),
    )
    res = backend.run()

    assert res.results["fail_stage"] == "resume"
    assert "input extra_inputs differs" in res.results["desc"]
    assert "window.tcl" in res.results["desc"]


def test_resume_refuses_an_input_added_since_the_checkpoints(tmp_path, monkeypatch):
    backend, artefacts = _backend(
        tmp_path, monkeypatch, stages=True, stop_after="global_route"
    )
    assert backend.run().results["result"] == "PASS"
    (first,) = _runs(artefacts)
    helper = _write(tmp_path / "window.tcl", "set a 1\n")

    backend, _ = _backend(
        tmp_path,
        monkeypatch,
        stages=True,
        resume=first.name,
        resume_from="detailed_route",
        pnr_cfg=_staged_cfg(tmp_path, helper),
    )
    res = backend.run()

    assert res.results["fail_stage"] == "resume"
    assert "input extra_inputs differs" in res.results["desc"]


def test_resume_refuses_an_input_moved_to_a_same_bytes_file(tmp_path, monkeypatch):
    helper = _write(tmp_path / "window.tcl", "set a 1\n")
    first = _stopped_run(tmp_path, monkeypatch, helper)
    moved = _write(tmp_path / "other/window.tcl", "set a 1\n")

    backend, _ = _backend(
        tmp_path,
        monkeypatch,
        stages=True,
        resume=first.name,
        resume_from="detailed_route",
        pnr_cfg=_staged_cfg(tmp_path, moved),
    )
    res = backend.run()

    assert res.results["fail_stage"] == "resume"
    desc = res.results["desc"]
    assert "input extra_inputs differs" in desc
    assert "other/window.tcl" in desc


def test_resume_accepts_an_input_named_by_another_spelling(tmp_path, monkeypatch):
    helper = _write(tmp_path / "window.tcl", "set a 1\n")
    first = _stopped_run(tmp_path, monkeypatch, helper)
    (tmp_path / "sub").mkdir()

    backend, _ = _backend(
        tmp_path,
        monkeypatch,
        stages=True,
        resume=first.name,
        resume_from="detailed_route",
        pnr_cfg=_staged_cfg(tmp_path, tmp_path / "sub/../window.tcl"),
    )
    res = backend.run()

    assert res.results["result"] == "PASS", res.results


# Hardened block


def test_inputs_join_the_abstract_config_only_when_set(tmp_path):
    platform = _flow_platform(_make_pdk_cfg(tmp_path), cts_buffer="BUF_X4")
    plain = pnr_abstract.abstract_config(_make_pnr_cfg(tmp_path), platform)
    assert "inputs" not in plain

    listed = pnr_abstract.abstract_config(
        _make_pnr_cfg(tmp_path, inputs=(str(tmp_path / "a.tcl"),)), platform
    )
    assert listed["inputs"][0].endswith("a.tcl")
    assert {k: v for k, v in listed.items() if k != "inputs"} == plain
    assert pnr_abstract.config_digest(listed) != pnr_abstract.config_digest(plain)


def test_editing_an_input_makes_a_hardened_block_stale(tmp_path):
    suite = _block_suite(tmp_path)
    assert _stale_changes(tmp_path, suite) == []

    helper = _write(tmp_path / "blk/window.tcl", "set a 1\n")
    suite.write_text(suite.read_text() + "    inputs: [window.tcl]\n")
    assert any("config changed (inputs)" in c for c in _stale_changes(tmp_path, suite))

    # Record the block as hardened with the input, then edit the input.
    manifest_path = tmp_path / "blk/artefacts/blk_pnr/abstract/abstract.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    run_cfg = PnrSuiteConfig(str(suite)).get_runs("blk_pnr")[0]
    config = pnr_abstract.abstract_config(run_cfg, _block_platform(tmp_path))
    manifest["config"] = {**config, "sha256": pnr_abstract.config_digest(config)}
    manifest["inputs"]["extra_inputs"] = [
        {"path": str(helper), "sha256": _sha("set a 1\n")}
    ]
    manifest_path.write_text(json.dumps(manifest))
    assert _stale_changes(tmp_path, suite) == []

    helper.write_text("set a 2\n")
    assert _stale_changes(tmp_path, suite) == [f"extra_inputs {helper} changed"]


def _harden_with_input(tmp_path, suite) -> Path:
    """Record the block as hardened with `inputs: [window.tcl]`; return the helper."""
    helper = _write(tmp_path / "blk/window.tcl", "set a 1\n")
    suite.write_text(suite.read_text() + "    inputs: [window.tcl]\n")
    manifest_path = tmp_path / "blk/artefacts/blk_pnr/abstract/abstract.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    run_cfg = PnrSuiteConfig(str(suite)).get_runs("blk_pnr")[0]
    config = pnr_abstract.abstract_config(run_cfg, _block_platform(tmp_path))
    manifest["config"] = {**config, "sha256": pnr_abstract.config_digest(config)}
    manifest["inputs"]["extra_inputs"] = [
        {"path": str(helper), "sha256": _sha("set a 1\n")}
    ]
    manifest_path.write_text(json.dumps(manifest))
    return helper


def test_a_deleted_input_reports_a_hardened_block_stale(tmp_path):
    suite = _block_suite(tmp_path)
    helper = _harden_with_input(tmp_path, suite)
    helper.unlink()

    assert _stale_changes(tmp_path, suite) == [f"extra_inputs {helper} is gone"]


def test_rb_pnr_accept_stale_takes_a_block_whose_input_is_gone(tmp_path, monkeypatch):
    helper = _harden_with_input(tmp_path, _block_suite(tmp_path))
    helper.unlink()

    backend, launched = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)
    refused = backend.run()
    assert refused.results["fail_stage"] == "setup"
    assert f"extra_inputs {helper} is gone" in refused.results["desc"]
    assert launched == []

    backend, launched = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)
    backend.accept_stale = True
    res = backend.run()
    assert res.is_pass(), res.results
    assert launched
    [row] = res.results["blocks"]
    assert row["stale"] is True
    assert row["changes"] == [f"extra_inputs {helper} is gone"]


def test_rb_synth_accept_stale_takes_a_block_whose_input_is_gone(tmp_path, monkeypatch):
    from rtl_buddy.tools import synth_yosys

    helper = _harden_with_input(tmp_path, _block_suite(tmp_path))
    helper.unlink()
    monkeypatch.setattr(
        synth_yosys.YosysSynth,
        "run",
        lambda self: MagicMock(results={"result": "PASS", "desc": "ok"}),
    )

    refused = _synth_runner(tmp_path, _BLOCK_YAML).run()
    assert refused.results["fail_stage"] == "setup"
    assert f"extra_inputs {helper} is gone" in refused.results["desc"]

    runner = _synth_runner(tmp_path, _BLOCK_YAML)
    runner.accept_stale = True
    accepted = runner.run()
    assert accepted.results["desc"] == "ok; stale block abstract(s) accepted: blk_top"
    assert accepted.results["blocks"][0]["changes"] == [
        f"extra_inputs {helper} is gone"
    ]
