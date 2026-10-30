"""Tests for `rb pnr --run-tag` and `rb pnr-export --run-tag`.

A tagged invocation writes its P&R runs under `artefacts/.runs/<tag>/` and locks that
tree, so it runs beside an untagged invocation or one under another tag. An untagged
invocation keeps the flat layout and the whole-tree lock.
"""

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from dataclasses import replace

from test_pnr_blocks import _block_platform, _block_suite, _ref, _write
from test_pnr_checkpoints import _backend, _pnr_cfg
from test_pnr_plan import _driver, _run, _suite, _synth_driver, _synth_run, _synth_yaml
from test_pnr_stages import _refusal, _stopped_at_global_route
from typer.testing import CliRunner

from rtl_buddy import artifact_lock
from rtl_buddy.artifact_lock import LOCK_FILENAME, ArtifactLocks
from rtl_buddy.config.pnr import PnrSuiteConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.pnr_results import PnrPassResults
from rtl_buddy.tools.artifact_paths import RUNS_DIRNAME, run_artifact_root
from rtl_buddy.tools import pnr_abstract
from rtl_buddy.tools import pnr_tag_inputs
from rtl_buddy.tools.pnr_abstract import BlockResolutionError, resolve_block
from rtl_buddy.tools.pnr_tag_inputs import BLOCK_COPIES_DIRNAME, TagInputs
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


@pytest.mark.parametrize("command", ["pnr", "pnr-export"])
def test_a_call_refused_by_a_busy_tag_leaves_the_holders_log_whole(
    pnr_project, locks, command
):
    tagged = run_artifact_root(pnr_project, "busy")
    locks(tagged)
    log = tagged / "rtl_buddy.log"
    log.write_text("the running call's log\n")

    clash = _rb(command, "a", "--run-tag", "busy")

    assert "another rtl-buddy run" in str(clash.exception)
    assert log.read_text() == "the running call's log\n"


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
    pnr_tag_inputs.mark_attempt(str(tagged))
    return tagged / "abstract"


def _source(tmp_path, tag="t", **kw):
    return TagInputs(tag, str(run_artifact_root(tmp_path / "top", tag)), **kw)


def _root_cfg(tmp_path):
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = _block_platform(tmp_path)
    return root_cfg


def _copies(source):
    return sorted(Path(source.root, BLOCK_COPIES_DIRNAME).iterdir())


def test_a_tagged_consumer_reads_a_copy_of_the_abstract_its_tag_hardened(tmp_path):
    suite = _block_suite(tmp_path)
    tagged = _tag_abstract(tmp_path, "t")
    source = _source(tmp_path)

    block = resolve_block(_ref(suite), source=source)

    assert Path(block.abstract_dir).parent == Path(source.root) / BLOCK_COPIES_DIRNAME
    assert Path(block.lef).read_text() == (tagged / "blk_top.lef").read_text()
    assert block.run_tag == "t"
    assert block.result_row()["run_tag"] == "t"


def test_a_tag_that_never_ran_the_block_reads_a_copy_of_the_untagged_abstract(
    tmp_path, caplog
):
    suite = _block_suite(tmp_path)
    source = _source(tmp_path)

    block = resolve_block(_ref(suite), source=source)

    manifest = tmp_path / "blk/artefacts/blk_pnr/abstract/abstract.manifest.json"
    digest = pnr_abstract.file_fingerprint(str(manifest), None)["sha256"]
    copy = Path(block.abstract_dir)
    assert copy.parent == Path(source.root) / BLOCK_COPIES_DIRNAME
    assert copy.name.startswith("blk_pnr-") and copy.name.endswith(digest[:16])
    assert (copy / "blk_top.lef").read_text() == (
        tmp_path / "blk/artefacts/blk_pnr/abstract/blk_top.lef"
    ).read_text()
    row = block.result_row()
    assert row["run_tag"] is None
    assert row["manifest_sha256"] == digest
    assert block.manifest_entry() == {
        "name": "blk_top",
        "pnr": "blk_pnr",
        "run_tag": None,
        "manifest_sha256": digest,
    }
    assert any(
        r.levelname == "WARNING" and "copy of its untagged abstract" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("state", ["failed", "cleared"])
def test_a_tag_that_ran_the_block_without_an_abstract_fails_naming_the_tag(
    tmp_path, state
):
    """A tagged block run that failed or was cleared leaves its run directory and no abstract; the untagged one is not used."""
    suite = _block_suite(tmp_path)
    run_dir = tmp_path / "blk/artefacts" / RUNS_DIRNAME / "t" / "blk_pnr"
    run_dir.mkdir(parents=True)
    pnr_tag_inputs.mark_attempt(str(run_dir))
    if state == "failed":
        (run_dir / "pnr.log").write_text("route failed\n")

    with pytest.raises(BlockResolutionError) as info:
        resolve_block(_ref(suite), source=_source(tmp_path))
    assert str(run_dir / "abstract") in str(info.value)
    assert "--run-tag t" in str(info.value)


def test_a_run_directory_only_rb_pnr_export_created_is_not_a_tagged_run(tmp_path):
    suite = _block_suite(tmp_path)
    (tmp_path / "blk/artefacts" / RUNS_DIRNAME / "t" / "blk_pnr").mkdir(parents=True)

    block = resolve_block(_ref(suite), source=_source(tmp_path))

    assert block.run_tag is None
    assert (
        Path(block.lef).read_text()
        == (tmp_path / "blk/artefacts/blk_pnr/abstract/blk_top.lef").read_text()
    )


def test_only_rb_pnr_marks_a_tagged_run_attempted(tmp_path, monkeypatch):
    """The backend `rb pnr-export` builds creates the run directory but no marker; `run` writes it, even when it fails at setup."""
    from rtl_buddy.tools import pnr_openroad

    backend, _ = _backend(
        tmp_path, monkeypatch, stages=True, run_tag="t", stop_after="floorplan"
    )
    marker = Path(backend.artefact_dir, pnr_tag_inputs.ATTEMPT_MARKER_NAME)
    assert marker.parent.is_dir() and not marker.exists()

    res = backend.run().results
    assert res["fail_stage"] == "setup" and "no synthesis netlist" in res["desc"]
    assert marker.is_file()

    _write(Path(pnr_openroad.synth_netlist_path(backend.pnr_cfg)), "module x;\n")
    backend, _ = _backend(
        tmp_path, monkeypatch, stages=True, run_tag="t", stop_after="floorplan"
    )
    res = backend.run().results
    assert res["result"] == "PASS", res
    assert marker.is_file()


def test_a_copy_from_another_project_reads_its_inputs_against_that_project(tmp_path):
    """A block in another project records its inputs relative to that project's root; its copy in the consumer's tree is not stale."""
    suite = _block_suite(tmp_path)
    (tmp_path / "blk/root_config.yaml").write_text("")
    (tmp_path / "top").mkdir()
    (tmp_path / "top/root_config.yaml").write_text("")
    manifest = tmp_path / "blk/artefacts/blk_pnr/abstract/abstract.manifest.json"
    record = json.loads(manifest.read_text())
    record["inputs"]["sdc"]["path"] = "blk.sdc"
    # The block's own project root enters its config.
    [run_cfg] = PnrSuiteConfig(str(suite)).get_runs("blk_pnr")
    config = pnr_abstract.abstract_config(run_cfg, _block_platform(tmp_path))
    record["config"] = {**config, "sha256": pnr_abstract.config_digest(config)}
    manifest.write_text(json.dumps(record))

    block = resolve_block(_ref(suite), source=_source(tmp_path))

    assert block.source_dir == str(tmp_path / "blk/artefacts/blk_pnr/abstract")
    assert pnr_abstract.block_changes(block, _root_cfg(tmp_path)) == []
    (tmp_path / "blk/blk.sdc").write_text("create_clock -period 5\n")
    assert pnr_abstract.block_changes(block, _root_cfg(tmp_path)) == [
        "sdc blk.sdc changed"
    ]


def test_a_missing_untagged_abstract_names_both_commands(tmp_path):
    suite = _block_suite(tmp_path, manifest=False)
    with pytest.raises(BlockResolutionError, match="has not run the block") as info:
        resolve_block(_ref(suite), source=_source(tmp_path))
    assert "rb pnr blk_pnr -c" in str(info.value)
    assert "--run-tag t" in str(info.value)


def test_an_untagged_consumer_never_reads_a_tagged_abstract(tmp_path):
    suite = _block_suite(tmp_path)
    _tag_abstract(tmp_path, "t")

    with pytest.raises(BlockResolutionError) as info:
        resolve_block(_ref(suite))
    assert RUNS_DIRNAME not in str(info.value)
    assert "--run-tag" not in str(info.value)


def _re_harden(tmp_path):
    """Republish the untagged abstract with a new LEF and a manifest recording it."""
    published = tmp_path / "blk/artefacts/blk_pnr/abstract"
    (published / "blk_top.lef").write_text("re-hardened\n")
    manifest = json.loads((published / "abstract.manifest.json").read_text())
    manifest["outputs"]["lef"]["sha256"] = pnr_abstract.file_fingerprint(
        str(published / "blk_top.lef"), None
    )["sha256"]
    (published / "abstract.manifest.json").write_text(json.dumps(manifest))


def test_an_untagged_re_harden_does_not_change_the_copy_a_tagged_call_reads(tmp_path):
    """Synthesis and P&R of one tagged call read one copy, whatever an untagged re-harden does meanwhile."""
    suite = _block_suite(tmp_path)
    source = _source(tmp_path)
    first = resolve_block(_ref(suite), source=source)
    lef = Path(first.lef).read_text()

    _re_harden(tmp_path)

    again = resolve_block(_ref(suite), source=source)
    assert again.abstract_dir == first.abstract_dir
    assert Path(again.lef).read_text() == lef
    # Staleness is judged on the views read, not on the re-hardened ones.
    assert not [
        c
        for c in pnr_abstract.block_changes(again, _root_cfg(tmp_path))
        if "abstract" in c
    ]


def test_copies_are_keyed_by_digest_and_never_overwritten(tmp_path):
    suite = _block_suite(tmp_path)
    first = resolve_block(_ref(suite), source=_source(tmp_path))
    lef = Path(first.lef).read_text()

    _re_harden(tmp_path)
    later = resolve_block(_ref(suite), source=_source(tmp_path))

    assert later.abstract_dir != first.abstract_dir
    assert Path(later.lef).read_text() == "re-hardened\n"
    assert Path(first.lef).read_text() == lef
    assert len(_copies(_source(tmp_path))) == 2


def test_a_saved_tagged_run_is_exported_with_the_copy_it_consumed(tmp_path):
    """`rb pnr-export --run-tag` reads the copy the run recorded, not the block's current abstract."""
    suite = _block_suite(tmp_path)
    run_dir = tmp_path / "top/artefacts" / RUNS_DIRNAME / "t" / "top_pnr"
    run_dir.mkdir(parents=True)
    consumed = resolve_block(_ref(suite), source=_source(tmp_path))
    pnr_tag_inputs.write_consumed_blocks(str(run_dir), "t", [consumed])

    _re_harden(tmp_path)
    pinned = pnr_tag_inputs.read_consumed_blocks(str(run_dir))
    exported = resolve_block(_ref(suite), source=_source(tmp_path, pinned=pinned))

    assert exported.abstract_dir == consumed.abstract_dir
    assert exported.manifest_sha256 == consumed.manifest_sha256
    assert len(_copies(_source(tmp_path))) == 1

    shutil.rmtree(consumed.abstract_dir)
    with pytest.raises(BlockResolutionError, match="is gone"):
        resolve_block(_ref(suite), source=_source(tmp_path, pinned=pinned))
    with pytest.raises(BlockResolutionError, match="records no abstract"):
        resolve_block(_ref(suite), source=_source(tmp_path, pinned={}))


@pytest.mark.parametrize("edit", ["deleted", "renamed", "not hardened"])
def test_a_saved_tagged_run_is_exported_whatever_became_of_the_block_run(
    tmp_path, edit
):
    """The recorded copy is all the export needs; the live block run only feeds the staleness check."""
    suite = _block_suite(tmp_path)
    run_dir = tmp_path / "top/artefacts" / RUNS_DIRNAME / "t" / "top_pnr"
    run_dir.mkdir(parents=True)
    consumed = resolve_block(_ref(suite), source=_source(tmp_path))
    pnr_tag_inputs.write_consumed_blocks(str(run_dir), "t", [consumed])
    if edit == "deleted":
        suite.unlink()
    elif edit == "renamed":
        suite.write_text(suite.read_text().replace("name: blk_pnr", "name: other"))
    else:
        suite.write_text(suite.read_text().replace("harden: true", "harden: false"))
    pinned = pnr_tag_inputs.read_consumed_blocks(str(run_dir))

    exported = resolve_block(_ref(suite), source=_source(tmp_path, pinned=pinned))

    assert exported.abstract_dir == consumed.abstract_dir
    assert exported.run_cfg is None
    assert pnr_abstract.block_changes(exported, _root_cfg(tmp_path)) == []
    with pytest.raises(BlockResolutionError):
        resolve_block(_ref(suite), source=_source(tmp_path))


def test_a_tagged_checkpoint_is_exported_with_the_blocks_its_invocation_consumed(
    tmp_path, monkeypatch
):
    """Each checkpoint run directory keeps its own record, which `--checkpoint` reads instead of the run's latest."""
    from rtl_buddy.tools import pnr_openroad

    recorded = []
    monkeypatch.setattr(
        OpenRoadPnr,
        "_record_consumed_blocks",
        lambda self, directory=None: recorded.append(directory),
    )
    backend, _ = _backend(
        tmp_path, monkeypatch, stages=True, run_tag="t", stop_after="floorplan"
    )
    _write(Path(pnr_openroad.synth_netlist_path(backend.pnr_cfg)), "module x;\n")
    assert backend.run().results["result"] == "PASS"
    [ckpt_run] = sorted(
        p
        for p in Path(backend.artefact_dir, "checkpoints").iterdir()
        if p.is_dir() and not p.is_symlink()
    )
    assert recorded == [None, str(ckpt_run)]

    read = []

    def _read(directory):
        read.append(directory)
        raise RuntimeError("read")

    monkeypatch.setattr(pnr_tag_inputs, "read_consumed_blocks", _read)
    for checkpoint in (f"{ckpt_run.name}/floorplan", None):
        backend, _ = _backend(tmp_path, monkeypatch, stages=True, run_tag="t")
        with pytest.raises(RuntimeError, match="read"):
            backend.export_only(checkpoint=checkpoint)
    assert read == [str(ckpt_run), backend.artefact_dir]


def test_a_copy_that_does_not_match_its_manifest_is_refused(tmp_path):
    suite = _block_suite(tmp_path)
    published = tmp_path / "blk/artefacts/blk_pnr/abstract"
    (published / "blk_top.lib").write_text("torn\n")

    with pytest.raises(BlockResolutionError, match="does not match its manifest"):
        resolve_block(_ref(suite), source=_source(tmp_path))


@pytest.mark.parametrize("tree", ["untagged", "tagged"])
def test_the_copy_is_taken_under_the_block_trees_lock(tmp_path, monkeypatch, tree):
    """A tagged call waits while another process holds the tree the block's abstract is in, then copies.

    The tagged case is a named run consuming a block from another `pnr.yaml`, whose tagged tree this call does not hold.
    """
    monkeypatch.setattr(artifact_lock, "TREE_LOCK_POLL_SEC", 0.05)
    suite = _block_suite(tmp_path)
    lock = tmp_path / "blk/artefacts" / LOCK_FILENAME
    if tree == "tagged":
        _tag_abstract(tmp_path, "t")
        lock = run_artifact_root(tmp_path / "blk", "t") / LOCK_FILENAME
        lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time\n"
            f"f = open({str(lock)!r}, 'a')\n"
            "fcntl.flock(f, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(1.5)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        started = time.monotonic()
        block = resolve_block(_ref(suite), source=_source(tmp_path))
        waited = time.monotonic() - started
    finally:
        holder.wait(timeout=10)
    assert waited >= 0.5
    assert Path(block.lef).is_file()
    assert block.run_tag == (None if tree == "untagged" else "t")


def _hold(lock: Path, then: str = "", *, mkdir: bool = False) -> subprocess.Popen:
    """A process that holds `lock` (creating its directory with `mkdir`), says `held`, then runs `then`."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, pathlib, sys, time\n"
            + (
                f"pathlib.Path({str(lock.parent)!r}).mkdir(parents=True)\n"
                if mkdir
                else ""
            )
            + f"f = open({str(lock)!r}, 'a')\n"
            "fcntl.flock(f, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n" + then,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "held"
    return holder


def test_a_same_tag_block_run_holding_its_tree_is_waited_for_before_the_marker_is_read(
    tmp_path, monkeypatch
):
    """A block run under the tag that holds its tree but has not marked it yet is not passed over for the untagged abstract."""
    monkeypatch.setattr(artifact_lock, "TREE_LOCK_POLL_SEC", 0.05)
    suite = _block_suite(tmp_path)
    tagged = run_artifact_root(tmp_path / "blk", "t") / "blk_pnr"
    shutil.copytree(tmp_path / "blk/artefacts/blk_pnr", tagged)
    marker = tagged / pnr_tag_inputs.ATTEMPT_MARKER_NAME
    holder = _hold(
        tagged.parent / LOCK_FILENAME,
        f"time.sleep(1)\npathlib.Path({str(marker)!r}).touch()\n",
    )
    try:
        block = resolve_block(_ref(suite), source=_source(tmp_path))
    finally:
        holder.wait(timeout=10)

    assert block.run_tag == "t"


def test_a_same_tag_block_run_that_creates_its_tree_meanwhile_is_waited_for(
    tmp_path, monkeypatch
):
    """The block's tagged tree is absent when the consumer starts; a same-tag block run creates and locks it before the consumer's first lock, and is waited for."""
    monkeypatch.setattr(artifact_lock, "TREE_LOCK_POLL_SEC", 0.05)
    suite = _block_suite(tmp_path)
    tagged_root = run_artifact_root(tmp_path / "blk", "t")
    assert not tagged_root.exists()
    flat = tmp_path / "blk/artefacts/blk_pnr"
    tagged = tagged_root / "blk_pnr"
    holders = []
    real = pnr_tag_inputs.tree_lock

    def _block_run_starts_first(root, **kw):
        if not holders:
            holders.append(
                _hold(
                    tagged_root / LOCK_FILENAME,
                    "time.sleep(1)\n"
                    f"import shutil; shutil.copytree({str(flat)!r}, {str(tagged)!r})\n"
                    f"pathlib.Path({str(tagged / pnr_tag_inputs.ATTEMPT_MARKER_NAME)!r}).touch()\n",
                    mkdir=True,
                )
            )
        return real(root, **kw)

    monkeypatch.setattr(pnr_tag_inputs, "tree_lock", _block_run_starts_first)
    try:
        block = resolve_block(_ref(suite), source=_source(tmp_path))
    finally:
        for holder in holders:
            holder.wait(timeout=10)

    assert block.run_tag == "t"


@pytest.mark.parametrize("pinned", [True, False])
def test_a_copy_whose_view_was_edited_in_place_is_refused(tmp_path, pinned):
    suite = _block_suite(tmp_path)
    run_dir = tmp_path / "top/artefacts" / RUNS_DIRNAME / "t" / "top_pnr"
    run_dir.mkdir(parents=True)
    consumed = resolve_block(_ref(suite), source=_source(tmp_path))
    pnr_tag_inputs.write_consumed_blocks(str(run_dir), "t", [consumed])
    Path(consumed.gds).write_text("another layout\n")
    source = _source(
        tmp_path,
        pinned=pnr_tag_inputs.read_consumed_blocks(str(run_dir)) if pinned else None,
    )

    with pytest.raises(BlockResolutionError, match="gds"):
        resolve_block(_ref(suite), source=source)


def test_a_tagged_synth_copies_its_blocks_before_locking_the_synthesis_tree(
    tmp_path, monkeypatch
):
    """A same-tag block run holding the block's tree, then waiting for the synthesis tree, does not deadlock a tagged `--synth` whose synthesis has `blocks:`."""
    monkeypatch.setattr(artifact_lock, "TREE_LOCK_POLL_SEC", 0.05)
    blk_suite = _block_suite(tmp_path)
    _tag_abstract(tmp_path, "t")
    top = tmp_path / "top"
    _synth_yaml(
        top / "synth.yaml",
        "top_s",
        blocks={"top_s": [("blk_top", "blk_pnr", "../blk/pnr.yaml")]},
    )
    suite = _suite(top / "pnr.yaml", _synth_run("top", "top_s"))
    rb, _ran, _synths = _synth_driver(monkeypatch, {}, {})
    inner = rb._do_synth_suite
    resolved = []

    def _synth(suite_cfg, *, tag_inputs=None, **kw):
        resolved.append(resolve_block(_ref(blk_suite), source=tag_inputs))
        return inner(suite_cfg, **kw)

    rb._do_synth_suite = _synth
    synth_lock = run_artifact_root(top) / LOCK_FILENAME
    synth_lock.parent.mkdir(parents=True)
    holder = _hold(
        run_artifact_root(tmp_path / "blk", "t") / LOCK_FILENAME,
        "time.sleep(0.5)\n"
        f"g = open({str(synth_lock)!r}, 'a')\n"
        "fcntl.flock(g, fcntl.LOCK_EX)\n"
        "print('both', flush=True)\n",
    )
    results = []
    call = threading.Thread(
        target=lambda: results.extend(
            rb._do_pnr_suite(suite, pnr_name="top", run_synth=True, run_tag="t")
        ),
        daemon=True,
    )
    try:
        call.start()
        call.join(timeout=20)
        deadlocked = call.is_alive()
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert not deadlocked
    assert results[0]["results"].is_pass(), results
    assert [b.run_tag for b in resolved] == ["t"]


def test_a_tagged_export_refuses_a_block_its_run_consumed_that_blocks_no_longer_lists(
    tmp_path, monkeypatch
):
    suite = _block_suite(tmp_path)
    backend, _ = _backend(tmp_path, monkeypatch, stages=True, run_tag="t")
    consumed = resolve_block(_ref(suite), source=_source(tmp_path))
    pnr_tag_inputs.write_consumed_blocks(backend.artefact_dir, "t", [consumed])

    res = backend.export_only().results

    assert res["fail_stage"] == "setup"
    assert "'blk_top' (pnr run 'blk_pnr')" in res["desc"]
    assert "no longer lists" in res["desc"]


@pytest.mark.parametrize(
    "check",
    ["technology", "stale"],
)
def test_fix_it_commands_carry_the_tag_of_the_abstract_they_name(tmp_path, check):
    suite = _block_suite(tmp_path)
    _tag_abstract(tmp_path, "t")
    block = resolve_block(_ref(suite), source=_source(tmp_path))
    if check == "technology":
        del block.manifest["technology"]
        lib = _write(tmp_path / "top/pdk/typ.lib", "library (typ) {}\n")
        with pytest.raises(BlockResolutionError, match="--run-tag t`"):
            pnr_abstract.check_technology(block, liberty=[str(lib)], tech_lef=None)
    else:
        (tmp_path / "blk/blk.sdc").write_text("changed\n")
        with pytest.raises(BlockResolutionError, match="--run-tag t`, or pass"):
            pnr_abstract.assess_blocks([block], _root_cfg(tmp_path), accept_stale=False)


def test_an_untagged_fix_it_command_has_no_tag(tmp_path):
    suite = _block_suite(tmp_path)
    block = resolve_block(_ref(suite), source=_source(tmp_path))
    (tmp_path / "blk/blk.sdc").write_text("changed\n")
    with pytest.raises(BlockResolutionError) as info:
        pnr_abstract.assess_blocks([block], _root_cfg(tmp_path), accept_stale=False)
    assert "--run-tag" not in str(info.value)


def test_a_block_skipped_in_a_tagged_call_blocks_its_top(tmp_path, monkeypatch):
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("blk", reglvl=2000),
        _run("top", [("b", "blk", None)]),
    )
    rb, ran = _driver(monkeypatch, {})

    results = {
        r["pnr_name"]: r for r in rb._do_pnr_suite(suite, reg_level=1000, run_tag="t")
    }

    assert ran == []
    top = results["top"]["results"].results
    assert top["fail_stage"] == "blocked"
    assert "no abstract under --run-tag t" in top["desc"]


def test_tagged_synth_and_pnr_share_one_tag_inputs_and_lock_synthesis_only_while_it_runs(
    tmp_path, monkeypatch
):
    _synth_yaml(tmp_path / "synth.yaml", "top_s")
    suite = _suite(tmp_path / "pnr.yaml", _synth_run("top", "top_s"))
    rb, ran, _synths = _synth_driver(monkeypatch, {}, {})
    seen = {}

    class _Runner:
        def __init__(self, *, name, tag_inputs=None, **_kw):
            self.name = name
            seen["pnr"] = tag_inputs

        def run(self):
            ran.append(self.name)
            # Synthesis is over: the untagged tree is free again.
            probe = ArtifactLocks()
            probe.acquire(tmp_path / "artefacts")
            probe.release_all()
            return PnrPassResults(name=f"{self.name}/results")

    inner = rb._do_synth_suite

    def _synth(suite_cfg, *, tag_inputs=None, **kw):
        seen["synth"] = tag_inputs
        with pytest.raises(FatalRtlBuddyError, match="another rtl-buddy run"):
            ArtifactLocks().acquire(tmp_path / "artefacts")
        return inner(suite_cfg, **kw)

    rb._do_synth_suite = _synth
    monkeypatch.setattr("rtl_buddy.rtl_buddy.PnrRunner", _Runner)

    results = rb._do_pnr_suite(suite, run_synth=True, run_tag="t")

    assert results[0]["results"].is_pass(), results
    assert ran == ["synth:top_s", "top"]
    assert seen["synth"] is seen["pnr"]
    assert seen["pnr"].run_tag == "t"
    assert rb._artifact_locks.acquired == []


def _synth_tree(tmp_path, *, netlist="module top; endmodule\n"):
    """A `synth.yaml` defining `s` beside a pnr.yaml, with its netlist and a relative filelist."""
    _synth_yaml(tmp_path / "synth.yaml", "s")
    out = tmp_path / "artefacts" / "s"
    out.mkdir(parents=True, exist_ok=True)
    (out / "synth_netlist.v").write_text(netlist)
    (out / "synth.f").write_text("+incdir+../../rtl\n../../rtl/top.sv\n")
    return out


def test_a_tagged_run_reads_a_copy_of_the_synthesis_netlist(tmp_path):
    out = _synth_tree(tmp_path)
    [cfg] = _suite(tmp_path / "pnr.yaml", _run("a")).get_runs("a")
    backend = OpenRoadPnr(
        name="a", pnr_cfg=cfg, suite_dir=str(tmp_path), root_cfg=None, run_tag="t"
    )

    netlist = backend._resolve_netlist_path()

    copy = Path(netlist).parent
    assert (
        copy.parent
        == run_artifact_root(tmp_path, "t") / pnr_tag_inputs.SYNTH_COPIES_DIRNAME
    )
    assert Path(netlist).read_text() == "module top; endmodule\n"
    assert (copy / "synth.f").read_text().splitlines() == [
        f"+incdir+{tmp_path / 'rtl'}",
        str(tmp_path / "rtl/top.sv"),
    ]
    # A re-synthesis clearing the netlist does not reach the copy.
    (out / "synth_netlist.v").unlink()
    assert backend._resolve_netlist_path() == netlist
    assert Path(netlist).is_file()


def test_an_untagged_run_reads_the_synthesis_netlist_in_place(tmp_path):
    out = _synth_tree(tmp_path)
    [cfg] = _suite(tmp_path / "pnr.yaml", _run("a")).get_runs("a")
    backend = OpenRoadPnr(name="a", pnr_cfg=cfg, suite_dir=str(tmp_path), root_cfg=None)

    assert backend._resolve_netlist_path() == str(out / "synth_netlist.v")


def test_a_tagged_run_without_a_netlist_fails_at_setup_and_never_reads_the_synthesis(
    tmp_path,
):
    out = _synth_tree(tmp_path)
    (out / "synth_netlist.v").unlink()
    [cfg] = _suite(tmp_path / "pnr.yaml", _run("a")).get_runs("a")
    backend = OpenRoadPnr(
        name="a", pnr_cfg=cfg, suite_dir=str(tmp_path), root_cfg=None, run_tag="t"
    )

    failure = backend._tagged_netlist_failure()

    assert failure.results["fail_stage"] == "setup"
    assert "rb synth s -c" in failure.results["desc"]
    tagged = run_artifact_root(tmp_path, "t")
    for path in (backend._resolve_netlist_path(), backend._synth_dir()):
        assert Path(path).is_relative_to(tagged) and not Path(path).exists()
    untagged = OpenRoadPnr(
        name="a", pnr_cfg=cfg, suite_dir=str(tmp_path), root_cfg=None
    )
    assert untagged._tagged_netlist_failure() is None
    # A synthesis that this call runs later is still copied.
    (out / "synth_netlist.v").write_text("module top; endmodule\n")
    assert backend._tagged_netlist_failure() is None
    assert Path(backend._resolve_netlist_path()).parent.parent == (
        tagged / pnr_tag_inputs.SYNTH_COPIES_DIRNAME
    )


def test_only_an_accepted_resume_rewrites_the_consumed_blocks_record(
    tmp_path, monkeypatch
):
    """A refused resume leaves the record of the run it refused to continue; an accepted one rewrites it after clearing."""
    from rtl_buddy.tools import pnr_openroad

    def _record(self, directory=None):
        if directory is None:
            Path(self.artefact_dir, pnr_tag_inputs.CONSUMED_BLOCKS_NAME).write_text(
                f"{self.resume}\n"
            )

    monkeypatch.setattr(pnr_openroad.OpenRoadPnr, "_record_consumed_blocks", _record)
    first = _stopped_at_global_route(tmp_path, monkeypatch)
    record = first.parent.parent / pnr_tag_inputs.CONSUMED_BLOCKS_NAME
    assert record.read_text() == "None\n"

    cfg = _pnr_cfg(tmp_path, stages=True)
    _refusal(
        tmp_path,
        monkeypatch,
        first.name,
        pnr_cfg=replace(cfg, floorplan=replace(cfg.floorplan, utilization=0.6)),
    )
    assert record.read_text() == "None\n"

    backend, _ = _backend(
        tmp_path, monkeypatch, resume=first.name, resume_from="detailed_route"
    )
    assert backend.run().results["result"] == "PASS"
    assert record.read_text() == f"{first.name}\n"


def test_tagged_synth_copies_the_netlist_before_releasing_the_synthesis_lock(
    tmp_path, monkeypatch
):
    _synth_yaml(tmp_path / "synth.yaml", "top_s")
    suite = _suite(tmp_path / "pnr.yaml", _synth_run("top", "top_s"))
    rb, ran, _synths = _synth_driver(monkeypatch, {}, {})
    copied_under_lock = []
    real = TagInputs.synth_dir

    def _synth_dir(self, path, name):
        probe = ArtifactLocks()
        try:
            probe.acquire(tmp_path / "artefacts", log_contention=False)
        except FatalRtlBuddyError:
            copied_under_lock.append(name)
        else:
            probe.release_all()
        return real(self, path, name)

    monkeypatch.setattr(TagInputs, "synth_dir", _synth_dir)

    rb._do_pnr_suite(suite, run_synth=True, run_tag="t")

    assert copied_under_lock == ["top_s"]
