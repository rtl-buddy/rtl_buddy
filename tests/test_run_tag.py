"""Tests for ``--run-tag``, the per-invocation artefact namespace (#541).

Two regression tiers of one checkout — one per simulator — have no serial
dependency, but #73's tree lock is workspace-wide and the per-test paths
carry no run identity, so the second run dies on the first suite it
reaches. ``--run-tag <name>`` moves a run's whole tree under
``artefacts/.runs/<tag>/``, which makes the lock per-tag and the paths
disjoint.

The invariant every test here exists to protect is that **an untagged run
does not move**: same directories, same lock file, same log, same result
envelope bytes. The shared build directory does not move either — it is
keyed on the compile fingerprint, so two tags that compile the same thing
must keep sharing one ``obj_dir``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.artifact_lock import LOCK_FILENAME, ArtifactLocks
from rtl_buddy.dispatch.argv import build_job_argv
from rtl_buddy.dispatch.argv import test_job_argv as _test_job_argv
from rtl_buddy.dispatch.base import BuildJobSpec
from rtl_buddy.dispatch.base import TestJobSpec as _TestJobSpec
from rtl_buddy.dispatch.retry import job_output_paths
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.exec_context import ExecutionContext
from rtl_buddy.graph.config_tier import default_graph_dir
from rtl_buddy.graph.results import RESULTS_OVERLAY_NAME, collect_results
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.result_io import write_result_json
from rtl_buddy.runner.test_results import TestResults as _TestResults
from rtl_buddy.tools.artifact_paths import (
    RUNS_DIRNAME,
    run_artifact_root,
    shared_build_dir,
    test_artifact_dir,
    validate_run_tag,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_run_tag")


def _last_json(output: str) -> dict:
    """The machine-mode envelope: the last non-empty stdout line."""
    lines = [line for line in output.splitlines() if line.strip()]
    return json.loads(lines[-1])


@pytest.fixture
def locks():
    """An ArtifactLocks manager that always drops its locks on teardown."""
    managers = []

    def make():
        m = ArtifactLocks()
        managers.append(m)
        return m

    yield make
    for m in managers:
        m.release_all()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_untagged_path_helpers_are_todays_layout():
    assert run_artifact_root("/tmp/suite") == Path("/tmp/suite/artefacts")
    assert test_artifact_dir("/tmp/suite", "basic") == Path(
        "/tmp/suite/artefacts/basic"
    )
    assert test_artifact_dir("/tmp/suite", "basic", run_id=7) == Path(
        "/tmp/suite/artefacts/basic/run-0007"
    )


def test_tagged_path_helpers_nest_under_the_runs_directory():
    assert run_artifact_root("/tmp/suite", "sim-a") == Path(
        "/tmp/suite/artefacts/.runs/sim-a"
    )
    assert test_artifact_dir("/tmp/suite", "basic", run_tag="sim-a") == Path(
        "/tmp/suite/artefacts/.runs/sim-a/basic"
    )
    assert test_artifact_dir(
        "/tmp/suite", "with spaces/slash", run_id=7, run_tag="sim-a"
    ) == Path("/tmp/suite/artefacts/.runs/sim-a/with_spaces_slash/run-0007")


def test_the_runs_directory_is_dot_prefixed():
    # Load-bearing, not cosmetic: every reader of the artefact tree already
    # skips dot names (the overlay scan's `_is_test_dir`, the `+incdir+`
    # walk prune), so a tag can never be mistaken for a test directory.
    assert RUNS_DIRNAME.startswith(".")


def test_shared_build_dir_is_not_namespaced_by_a_tag():
    # The point of the namespace is separate *results*, not separate
    # compiles: the compile key already tells two toolchains apart, so two
    # tags that compile the same thing must reuse one obj_dir.
    assert shared_build_dir("/tmp/suite", "cafe0123") == Path(
        "/tmp/suite/artefacts/.shared-builds/obj_dir_cafe0123"
    )
    assert RUNS_DIRNAME not in shared_build_dir("/tmp/suite", "cafe0123").parts


@pytest.mark.parametrize("tag", ["a", "sim-a", "nightly_2026.09.18", "A-1.b_2"])
def test_valid_run_tags_pass_through_unchanged(tag):
    assert validate_run_tag(tag) == tag


def test_no_tag_passes_through_as_none():
    assert validate_run_tag(None) is None


@pytest.mark.parametrize(
    "tag",
    [
        "",
        ".",
        "..",
        "...",
        "a/b",
        "a\\b",
        "../escape",
        "with space",
        "tag:colon",
        "x" * 65,
    ],
)
def test_invalid_run_tags_are_rejected_loudly(tag):
    with pytest.raises(FatalRtlBuddyError, match="--run-tag"):
        validate_run_tag(tag)


def test_an_invalid_tag_is_rejected_before_a_path_is_built():
    with pytest.raises(FatalRtlBuddyError, match="--run-tag"):
        run_artifact_root("/tmp/suite", "../escape")


# ---------------------------------------------------------------------------
# Execution context
# ---------------------------------------------------------------------------


def test_execution_context_moves_the_artifact_root_and_log_under_the_tag(tmp_path):
    config = tmp_path / "suite" / "tests.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("rtl-buddy-filetype: test_config\n")

    flat = ExecutionContext.for_command(invocation_cwd=tmp_path, primary_config=config)
    assert flat.run_tag is None
    assert flat.artifact_root == config.parent / "artefacts"
    assert flat.log_path == config.parent / "rtl_buddy.log"

    tagged = ExecutionContext.for_command(
        invocation_cwd=tmp_path, primary_config=config, run_tag="sim-a"
    )
    assert tagged.run_tag == "sim-a"
    assert tagged.artifact_root == config.parent / "artefacts" / RUNS_DIRNAME / "sim-a"
    # The log moves too: it is opened for writing and the first open
    # truncates, so two concurrent runs sharing one path erase each other.
    assert tagged.log_path == tagged.artifact_root / "rtl_buddy.log"


def test_execution_context_for_dir_namespaces_the_same_way(tmp_path):
    tagged = ExecutionContext.for_dir(
        invocation_cwd=tmp_path, command_root=tmp_path, run_tag="sim-a"
    )
    assert tagged.artifact_root == tmp_path / "artefacts" / RUNS_DIRNAME / "sim-a"


def test_an_explicit_artifact_root_is_namespaced_too(tmp_path):
    # The reserved override has to mean the same thing wherever the tree
    # was redirected to, or a future --artifact-root would quietly put two
    # concurrent runs back onto one lock.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert (
        ExecutionContext.for_dir(
            invocation_cwd=tmp_path, command_root=tmp_path, artifact_root=scratch
        ).artifact_root
        == scratch
    )
    assert (
        ExecutionContext.for_dir(
            invocation_cwd=tmp_path,
            command_root=tmp_path,
            artifact_root=scratch,
            run_tag="sim-a",
        ).artifact_root
        == scratch / RUNS_DIRNAME / "sim-a"
    )


# ---------------------------------------------------------------------------
# The tree lock is per-tag
# ---------------------------------------------------------------------------


def test_two_tags_in_one_suite_do_not_contend(tmp_path, locks):
    suite = tmp_path / "suite"
    locks().acquire(run_artifact_root(suite, "sim-a"), command="regression")
    locks().acquire(run_artifact_root(suite, "sim-b"), command="regression")
    assert (run_artifact_root(suite, "sim-a") / LOCK_FILENAME).is_file()
    assert (run_artifact_root(suite, "sim-b") / LOCK_FILENAME).is_file()


def test_the_same_tag_in_one_suite_still_contends(tmp_path, locks):
    suite = tmp_path / "suite"
    locks().acquire(run_artifact_root(suite, "sim-a"), command="regression")
    with pytest.raises(FatalRtlBuddyError, match="another rtl-buddy run"):
        locks().acquire(run_artifact_root(suite, "sim-a"), command="regression")


def test_a_tagged_run_does_not_contend_with_an_untagged_one(tmp_path, locks):
    suite = tmp_path / "suite"
    locks().acquire(run_artifact_root(suite), command="regression")
    locks().acquire(run_artifact_root(suite, "sim-a"), command="regression")


# ---------------------------------------------------------------------------
# CLI: the layout, and the untagged layout staying put
# ---------------------------------------------------------------------------


def test_cli_run_tag_namespaces_the_whole_tree(minimal_project: Path):
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["--machine", "-E", "comp", "test", "basic", "--run-tag", "sim-a"]
    )
    assert result.exit_code == 0, result.output
    rb._artifact_locks.release_all()

    tagged = minimal_project / "artefacts" / RUNS_DIRNAME / "sim-a"
    assert (tagged / "basic" / "result.json").is_file()
    assert (tagged / LOCK_FILENAME).is_file()
    assert (tagged / "rtl_buddy.log").is_file()
    # Nothing landed in the flat tree, including its lock and its log.
    assert not (minimal_project / "artefacts" / "basic").exists()
    assert not (minimal_project / "artefacts" / LOCK_FILENAME).exists()
    assert not (minimal_project / "rtl_buddy.log").exists()
    # ...and the machine envelope says which tree the result came from.
    assert _last_json(result.output)["meta"]["run_tag"] == "sim-a"


def test_cli_without_a_run_tag_keeps_the_flat_layout(minimal_project: Path):
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["--machine", "-E", "comp", "test", "basic"])
    assert result.exit_code == 0, result.output
    rb._artifact_locks.release_all()

    assert (minimal_project / "artefacts" / "basic" / "result.json").is_file()
    assert (minimal_project / "artefacts" / LOCK_FILENAME).is_file()
    assert (minimal_project / "rtl_buddy.log").is_file()
    assert not (minimal_project / "artefacts" / RUNS_DIRNAME).exists()
    assert _last_json(result.output)["meta"]["run_tag"] is None


def test_cli_regression_namespaces_every_suite_it_visits(minimal_project: Path):
    # The #541 case: a whole tier under one tag. The manifest root gets a
    # tagged tree too, because that is where the head's own lock and log go.
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        # `-M debug` because the stub builder in the fixture only declares
        # that mode; `rb regression` would otherwise default to `reg`.
        ["--machine", "-E", "comp", "-M", "debug", "regression", "--run-tag", "sim-a"],
    )
    assert result.exit_code == 0, result.output
    rb._artifact_locks.release_all()

    tagged = minimal_project / "artefacts" / RUNS_DIRNAME / "sim-a"
    assert (tagged / "basic" / "result.json").is_file()
    assert (tagged / LOCK_FILENAME).is_file()
    assert not (minimal_project / "artefacts" / "basic").exists()


def test_cli_run_is_blocked_only_by_a_run_holding_the_same_tag(
    minimal_project: Path, locks
):
    # The lock the second simulator's run used to die on, now per-tag.
    locks().acquire(run_artifact_root(minimal_project, "sim-a"), command="regression")

    runner, rb = _runner()
    ok = runner.invoke(
        rb.app, ["--machine", "-E", "comp", "test", "basic", "--run-tag", "sim-b"]
    )
    assert ok.exit_code == 0, ok.output
    rb._artifact_locks.release_all()

    runner, rb = _runner()
    clash = runner.invoke(
        rb.app, ["--machine", "-E", "comp", "test", "basic", "--run-tag", "sim-a"]
    )
    assert clash.exit_code != 0
    assert "another rtl-buddy run" in str(clash.exception)


def test_cli_rejects_an_invalid_run_tag_before_writing_anything(minimal_project: Path):
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["--machine", "-E", "comp", "test", "basic", "--run-tag", "../escape"]
    )
    assert result.exit_code != 0
    assert not (minimal_project / "artefacts").exists()


def test_the_result_envelope_names_the_tag_only_when_there_is_one(minimal_project):
    runner, rb = _runner()
    assert (
        runner.invoke(
            rb.app, ["--machine", "-E", "comp", "test", "basic", "--run-tag", "sim-a"]
        ).exit_code
        == 0
    )
    rb._artifact_locks.release_all()
    runner, rb = _runner()
    assert (
        runner.invoke(rb.app, ["--machine", "-E", "comp", "test", "basic"]).exit_code
        == 0
    )
    rb._artifact_locks.release_all()

    tagged = json.loads(
        (
            minimal_project
            / "artefacts"
            / RUNS_DIRNAME
            / "sim-a"
            / "basic"
            / "result.json"
        ).read_text()
    )
    assert tagged["run_tag"] == "sim-a"
    flat = json.loads(
        (minimal_project / "artefacts" / "basic" / "result.json").read_text()
    )
    # Absent, not null: an untagged run's envelope stays byte-identical to
    # a pre-#541 one.
    assert "run_tag" not in flat


def test_write_result_json_omits_the_key_without_a_tag(tmp_path):
    results = _TestResults(name="t", results={"result": "PASS", "desc": "ok"})
    path = write_result_json(
        tmp_path / "result.json", test_name="t", run_id=None, results=results
    )
    assert "run_tag" not in json.loads(path.read_text())


# ---------------------------------------------------------------------------
# rb graph results
# ---------------------------------------------------------------------------


@pytest.fixture
def results_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The config-tier fixture, runnable as a project root."""
    target = tmp_path / "project"
    shutil.copytree(_FIXTURES / "graph_config_tier", target)
    shutil.copy(_FIXTURES / "minimal_project" / "root_config.yaml", target)
    for name in ("blk_a", "blk_b"):
        (target / "design" / name / f"{name}.sv").write_text(
            f"module {name} (input logic clk);\nendmodule\n"
        )
    monkeypatch.chdir(target)
    return target


def _seed_tagged_run(project: Path, test: str, *, run_tag: str | None, status: str):
    directory = test_artifact_dir(project / "verif" / "blk_a", test, run_tag=run_tag)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "test.log").write_text(f"{status}\n")
    write_result_json(
        directory / "result.json",
        test_name=test,
        run_id=None,
        results=_TestResults(name=test, results={"result": status, "desc": "ok"}),
        run_token="tok-1",
        run_tag=run_tag,
    )
    return directory


def test_collect_results_reads_only_the_named_run_tree(results_project: Path):
    _seed_tagged_run(results_project, "t_basic", run_tag=None, status="FAIL")
    _seed_tagged_run(results_project, "t_basic", run_tag="sim-a", status="PASS")

    flat = collect_results(results_project, coverage=False)
    tagged = collect_results(results_project, coverage=False, run_tag="sim-a")

    # One entry each, and the statuses do not cross over. The flat scan in
    # particular must not report `.runs` as a test that ran.
    assert [e["status"] for e in flat.entries.values()] == ["FAIL"]
    assert [e["status"] for e in tagged.entries.values()] == ["PASS"]
    assert not any(RUNS_DIRNAME in node_id for node_id in flat.entries)


def test_default_graph_dir_follows_the_tag_but_graph_json_does_not():
    assert default_graph_dir("/p") == Path("/p/artefacts/graph")
    assert default_graph_dir("/p", "sim-a") == Path("/p/artefacts/.runs/sim-a/graph")


def test_cli_graph_results_writes_the_tagged_overlay(results_project: Path):
    _seed_tagged_run(results_project, "t_basic", run_tag="sim-a", status="PASS")
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["--machine", "graph", "results", "--run-tag", "sim-a"]
    )
    assert result.exit_code == 0, result.output
    rb._artifact_locks.release_all()

    overlay = (
        results_project
        / "artefacts"
        / RUNS_DIRNAME
        / "sim-a"
        / "graph"
        / RESULTS_OVERLAY_NAME
    )
    assert overlay.is_file()
    assert not (results_project / "artefacts" / "graph" / RESULTS_OVERLAY_NAME).exists()
    payload = _last_json(result.output)["payload"]
    assert payload["overlay"] == str(
        Path("artefacts") / RUNS_DIRNAME / "sim-a" / "graph" / RESULTS_OVERLAY_NAME
    )


# ---------------------------------------------------------------------------
# Dispatch: head and job agree on the tree
# ---------------------------------------------------------------------------


def _test_spec(**kwargs) -> _TestJobSpec:
    return _TestJobSpec(
        test_name="basic",
        suite_dir="/tmp/suite",
        test_config_path="/tmp/suite/tests.yaml",
        result_json=Path("/tmp/suite/artefacts/basic/dispatch/result-single.json"),
        **kwargs,
    )


def test_job_argv_carries_the_tag_only_when_there_is_one():
    assert "--run-tag" not in _test_job_argv(_test_spec())
    argv = _test_job_argv(_test_spec(run_tag="sim-a"))
    assert argv[argv.index("--run-tag") + 1] == "sim-a"

    build = BuildJobSpec(
        suite_dir="/tmp/suite", test_config_path="/tmp/suite/tests.yaml"
    )
    assert "--run-tag" not in build_job_argv(build)
    build.run_tag = "sim-a"
    argv = build_job_argv(build)
    assert argv[argv.index("--run-tag") + 1] == "sim-a"


def test_retry_reads_the_tagged_runs_sim_logs():
    flat = job_output_paths(_test_spec())
    assert Path("/tmp/suite/artefacts/basic/test.log") in flat

    tagged = job_output_paths(_test_spec(run_tag="sim-a"))
    assert Path("/tmp/suite/artefacts/.runs/sim-a/basic/test.log") in tagged
    assert Path("/tmp/suite/artefacts/basic/test.log") not in tagged
