"""Tests for `rb pnr --run-tag` and `rb pnr-export --run-tag`.

A tagged invocation writes its P&R runs under `artefacts/.runs/<tag>/` and locks that
tree, so it runs beside an untagged invocation or one under another tag. An untagged
invocation keeps the flat layout and the whole-tree lock.
"""

import json
import shutil
from pathlib import Path

import pytest
from test_pnr_blocks import _block_suite, _ref
from test_pnr_plan import _driver, _run, _suite
from typer.testing import CliRunner

from rtl_buddy.artifact_lock import LOCK_FILENAME, ArtifactLocks
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.pnr_results import PnrPassResults
from rtl_buddy.tools.artifact_paths import RUNS_DIRNAME, run_artifact_root
from rtl_buddy.tools.pnr_abstract import BlockResolutionError, resolve_block
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr


@pytest.fixture
def locks():
    """ArtifactLocks managers standing in for running invocations; released on teardown."""
    managers = []

    def hold(root):
        m = ArtifactLocks()
        m.acquire(root, command="pnr")
        managers.append(m)

    yield hold
    for m in managers:
        m.release_all()


@pytest.fixture
def pnr_project(minimal_project, monkeypatch):
    """The minimal project with a two-run `pnr.yaml` whose OpenROAD run writes a marker
    into its artefact directory and passes.
    """
    _suite(minimal_project / "pnr.yaml", _run("a"), _run("b"))

    def _run_backend(self):
        Path(self.artefact_dir, "marker").write_text(self.run_tag or "")
        return PnrPassResults(name=f"{self.pnr_cfg.get_name()}/results")

    def _export(self, **_kw):
        Path(self.artefact_dir, "exported").write_text(self.run_tag or "")
        return PnrPassResults(name=f"{self.pnr_cfg.get_name()}/results")

    monkeypatch.setattr(OpenRoadPnr, "run", _run_backend)
    monkeypatch.setattr(OpenRoadPnr, "export_only", _export)
    return minimal_project


def _rb(*args):
    rb = RtlBuddy(name="test_pnr_run_tag")
    try:
        return CliRunner().invoke(rb.app, ["--machine", *args])
    finally:
        rb._artifact_locks.release_all()


def _envelope(output: str) -> dict:
    lines = [line for line in output.splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_untagged_rb_pnr_keeps_the_flat_layout_and_lock(pnr_project):
    result = _rb("pnr", "a")
    assert result.exit_code == 0, result.output

    artefacts = pnr_project / "artefacts"
    assert (artefacts / "a" / "marker").read_text() == ""
    assert (artefacts / LOCK_FILENAME).is_file()
    assert (pnr_project / "rtl_buddy.log").is_file()
    assert not (artefacts / RUNS_DIRNAME).exists()
    assert _envelope(result.output)["meta"]["run_tag"] is None


def test_a_tagged_rb_pnr_writes_its_own_tree(pnr_project):
    result = _rb("pnr", "a", "--run-tag", "util60")
    assert result.exit_code == 0, result.output

    tagged = pnr_project / "artefacts" / RUNS_DIRNAME / "util60"
    assert (tagged / "a" / "marker").read_text() == "util60"
    assert (tagged / LOCK_FILENAME).is_file()
    assert (tagged / "rtl_buddy.log").is_file()
    assert not (pnr_project / "artefacts" / "a").exists()
    assert not (pnr_project / "artefacts" / LOCK_FILENAME).exists()
    assert not (pnr_project / "rtl_buddy.log").exists()
    assert _envelope(result.output)["meta"]["run_tag"] == "util60"


def test_a_tagged_repeat_of_a_run_completes_beside_the_running_untagged_one(
    pnr_project, locks
):
    locks(run_artifact_root(pnr_project))

    repeat = _rb("pnr", "a", "--run-tag", "rep1")
    assert repeat.exit_code == 0, repeat.output
    assert (pnr_project / "artefacts/.runs/rep1/a/marker").is_file()

    # Without a tag, even a different run still needs the whole tree.
    other = _rb("pnr", "b")
    assert other.exit_code != 0
    assert "another rtl-buddy run" in str(other.exception)
    assert not (pnr_project / "artefacts" / "b").exists()


def test_different_runs_under_different_tags_complete_and_a_colliding_tag_is_refused(
    pnr_project, locks
):
    locks(run_artifact_root(pnr_project, "sweep-a"))

    ok = _rb("pnr", "b", "--run-tag", "sweep-b")
    assert ok.exit_code == 0, ok.output
    assert (pnr_project / "artefacts/.runs/sweep-b/b/marker").is_file()

    clash = _rb("pnr", "a", "--run-tag", "sweep-a")
    assert clash.exit_code != 0
    assert "another rtl-buddy run" in str(clash.exception)
    assert not (pnr_project / "artefacts/.runs/sweep-a/a").exists()


def test_an_invalid_run_tag_is_refused_before_anything_is_written(pnr_project):
    result = _rb("pnr", "a", "--run-tag", "../escape")
    assert result.exit_code != 0
    assert not (pnr_project / "artefacts").exists()


def test_rb_pnr_export_reads_the_tagged_result(pnr_project):
    assert _rb("pnr", "a", "--run-tag", "util60").exit_code == 0

    result = _rb("pnr-export", "a", "--run-tag", "util60")
    assert result.exit_code == 0, result.output
    assert (pnr_project / "artefacts/.runs/util60/a/exported").read_text() == "util60"
    assert not (pnr_project / "artefacts" / "a").exists()


def test_the_backend_artefact_directory_moves_only_under_a_tag(tmp_path):
    suite = _suite(tmp_path / "pnr.yaml", _run("a"))
    [cfg] = suite.get_runs("a")

    flat = OpenRoadPnr(name="a", pnr_cfg=cfg, suite_dir=str(tmp_path), root_cfg=None)
    assert flat.artefact_dir == str(Path(tmp_path) / "artefacts" / "a")

    tagged = OpenRoadPnr(
        name="a", pnr_cfg=cfg, suite_dir=str(tmp_path), root_cfg=None, run_tag="t"
    )
    assert tagged.artefact_dir == str(tmp_path / "artefacts" / RUNS_DIRNAME / "t" / "a")


def test_a_tagged_whole_suite_locks_and_writes_pulled_in_suites_under_the_tag(
    tmp_path, monkeypatch
):
    other = tmp_path / "blk" / "pnr.yaml"
    _suite(other, _run("blk_pnr"))
    suite = _suite(
        tmp_path / "top" / "pnr.yaml",
        _run("top", [("b", "blk_pnr", "../blk/pnr.yaml")]),
    )
    rb, ran = _driver(monkeypatch, {})
    tags = []

    class _Runner:
        def __init__(self, *, name, run_tag=None, **_kw):
            self.name = name
            tags.append(run_tag)

        def run(self):
            ran.append(self.name)
            return PnrPassResults(name=f"{self.name}/results")

    monkeypatch.setattr("rtl_buddy.rtl_buddy.PnrRunner", _Runner)

    rb._do_pnr_suite(suite, run_tag="t")

    assert ran == ["blk_pnr", "top"]
    assert tags == ["t", "t"]
    assert rb._artifact_locks.acquired == [
        run_artifact_root(other.resolve().parent, "t")
    ]


def _tag_abstract(tmp_path, tag):
    """Move the block's published run directory under `tag`."""
    flat = tmp_path / "blk/artefacts/blk_pnr"
    tagged = tmp_path / "blk/artefacts" / RUNS_DIRNAME / tag / "blk_pnr"
    tagged.parent.mkdir(parents=True)
    shutil.move(flat, tagged)
    return tagged / "abstract"


def test_a_tagged_consumer_reads_the_abstract_its_tag_hardened(tmp_path):
    suite = _block_suite(tmp_path)
    tagged = _tag_abstract(tmp_path, "t")

    block = resolve_block(_ref(suite), run_tag="t")

    assert block.abstract_dir == str(tagged)
    assert block.lef == str(tagged / "blk_top.lef")


def test_a_tagged_consumer_falls_back_to_the_untagged_abstract(tmp_path):
    suite = _block_suite(tmp_path)

    block = resolve_block(_ref(suite), run_tag="t")

    assert block.abstract_dir == str(tmp_path / "blk/artefacts/blk_pnr/abstract")


def test_an_untagged_consumer_never_reads_a_tagged_abstract(tmp_path):
    suite = _block_suite(tmp_path)
    _tag_abstract(tmp_path, "t")

    with pytest.raises(BlockResolutionError) as info:
        resolve_block(_ref(suite))
    assert RUNS_DIRNAME not in str(info.value)

    with pytest.raises(BlockResolutionError, match=" or ") as info:
        resolve_block(_ref(suite), run_tag="u")
    assert f"{RUNS_DIRNAME}/u/blk_pnr/abstract" in str(info.value)
