"""``--run-tag``: a per-invocation artefact-tree namespace (#541).

Two runs in one checkout — a nightly gating on two simulators, say — have
no serial dependency under ``--dispatch``, but they collide on paths that
carry no run identity: ``artefacts/<test>/result.json`` is one file per
test, and the exclusive artefact-tree lock (#73) makes the second run die
rather than interleave with the first.

``--run-tag`` moves the whole tree to ``artefacts/.runs/<tag>/``, lock
included, so the two runs share nothing. Unset, every path and the lock are
exactly what they were.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from rtl_buddy.artifact_lock import LOCK_FILENAME, ArtifactLocks
from rtl_buddy.dispatch.argv import build_job_argv, test_job_argv as _test_job_argv
from rtl_buddy.dispatch.base import BuildJobSpec, TestJobSpec as _TestJobSpec
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.exec_context import ExecutionContext
from rtl_buddy.graph import query as graph_query
from rtl_buddy.graph import results as graph_results
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.tools.coverage import CoverageReporter
from rtl_buddy.tools.artifact_paths import (
    normalize_run_tag,
    shared_build_dir,
    suite_artifact_root,
    test_artifact_dir,
)


class _DummyCovRootCfg:
    """The two accessors `CoverageReporter` reaches for on construction."""

    def __init__(self, project_root):
        self._project_root = str(project_root)

    def get_project_rootdir(self):
        return self._project_root

    def get_rtl_builder_cfg(self):
        return SimpleNamespace(
            get_simulator_family=lambda: "verilator",
            get_name=lambda: "verilator",
        )


_REPO = Path(__file__).resolve().parent.parent
_SHIMS = _REPO / "tests" / "dispatch_shims"
_FIXTURE = _REPO / "tests" / "fixtures" / "dispatch_project"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_an_untagged_tree_is_byte_for_byte_todays_layout(tmp_path):
    assert suite_artifact_root(tmp_path) == tmp_path / "artefacts"
    assert test_artifact_dir(tmp_path, "basic") == tmp_path / "artefacts" / "basic"
    assert (
        test_artifact_dir(tmp_path, "basic", run_id=7)
        == tmp_path / "artefacts" / "basic" / "run-0007"
    )
    assert (
        shared_build_dir(tmp_path, "abc123")
        == tmp_path / "artefacts" / ".shared-builds" / "obj_dir_abc123"
    )


def test_a_tag_namespaces_the_whole_tree_not_just_the_test_dirs(tmp_path):
    """Per-test dirs, shared builds and everything else move together.

    Sharing ``.shared-builds`` between tags would save a compile when two
    runs use one simulator, but a second head may rebuild a key the first
    one's fan-out is gated on, which since #539 fails that run rather than
    recompiling. The build moves with the tree.
    """
    root = tmp_path / "artefacts" / ".runs" / "vcs"
    assert suite_artifact_root(tmp_path, "vcs") == root
    assert test_artifact_dir(tmp_path, "basic", run_tag="vcs") == root / "basic"
    assert (
        test_artifact_dir(tmp_path, "basic", run_id=7, run_tag="vcs")
        == root / "basic" / "run-0007"
    )
    assert (
        shared_build_dir(tmp_path, "abc123", run_tag="vcs")
        == root / ".shared-builds" / "obj_dir_abc123"
    )


def test_two_tags_share_no_path(tmp_path):
    left = suite_artifact_root(tmp_path, "vcs")
    right = suite_artifact_root(tmp_path, "verilator")
    assert left != right
    assert not str(left).startswith(str(right))
    assert not str(right).startswith(str(left))


def test_a_tag_is_sanitized_like_every_other_artefact_component():
    assert normalize_run_tag("vcs/nightly") == "vcs_nightly"
    assert normalize_run_tag("  spaced  ") == "spaced"
    # Leading and trailing ._- go, so a tag can never read as one of this
    # module's dot-directories or hide from an `ls`.
    assert normalize_run_tag(".hidden.") == "hidden"


@pytest.mark.parametrize("tag", ["", "...", "---", "_", "x" * 65])
def test_a_tag_that_names_no_directory_is_refused(tag):
    with pytest.raises(FatalRtlBuddyError):
        normalize_run_tag(tag)


def test_the_execution_context_puts_the_lock_on_the_tagged_root(tmp_path):
    (tmp_path / "tests.yaml").write_text("tests:\n")
    ctx = ExecutionContext.for_command(
        invocation_cwd=tmp_path,
        primary_config=tmp_path / "tests.yaml",
        run_tag="vcs",
    )
    assert ctx.artifact_root == tmp_path / "artefacts" / ".runs" / "vcs"
    assert ctx.run_tag == "vcs"

    untagged = ExecutionContext.for_command(
        invocation_cwd=tmp_path, primary_config=tmp_path / "tests.yaml"
    )
    assert untagged.artifact_root == tmp_path / "artefacts"
    assert untagged.run_tag is None


# ---------------------------------------------------------------------------
# The lock, which is the thing that stopped the second run
# ---------------------------------------------------------------------------


def test_a_held_untagged_lock_does_not_stop_a_tagged_run(minimal_project: Path):
    """The whole point: #73 stops being workspace-wide.

    The untagged tree is locked, as a concurrent regression would leave it,
    and a tagged command still runs — into its own tree, with its own lock.
    """
    holder = ArtifactLocks()
    holder.acquire(minimal_project / "artefacts", command="regression")
    try:
        rb = RtlBuddy(name="test_run_tag")
        result = CliRunner().invoke(rb.app, ["--run-tag", "vcs", "test", "--list"])
        assert result.exit_code == 0, result.output
        rb._artifact_locks.release_all()
    finally:
        holder.release_all()


def test_two_tags_take_two_locks(tmp_path):
    locks = ArtifactLocks()
    other = ArtifactLocks()
    try:
        locks.acquire(suite_artifact_root(tmp_path, "vcs"), command="regression")
        # A different tag is a different tree, so it is not contended...
        other.acquire(suite_artifact_root(tmp_path, "verilator"), command="regression")
        # ...but the same tag still is.
        with pytest.raises(FatalRtlBuddyError):
            ArtifactLocks().acquire(suite_artifact_root(tmp_path, "vcs"))
    finally:
        locks.release_all()
        other.release_all()
    assert (tmp_path / "artefacts" / ".runs" / "vcs" / LOCK_FILENAME).is_file()


# ---------------------------------------------------------------------------
# Where the tag is refused
# ---------------------------------------------------------------------------


def test_a_command_that_does_not_thread_the_tag_refuses_it(minimal_project: Path):
    """Loud, because the alternative is moving a command's LOCK without
    moving its outputs — `rb synth` writes `artefacts/<synth>` directly."""
    rb = RtlBuddy(name="test_run_tag")
    result = CliRunner().invoke(rb.app, ["--run-tag", "vcs", "synth", "--list"])
    assert result.exit_code != 0
    assert "--run-tag is not supported" in str(result.exception)


def test_only_graph_results_takes_the_tag_inside_the_graph_group(minimal_project: Path):
    """`graph build` writes `artefacts/graph/`, which no tag moves."""
    rb = RtlBuddy(name="test_run_tag")
    result = CliRunner().invoke(rb.app, ["--run-tag", "vcs", "graph", "build"])
    assert result.exit_code != 0
    assert "'rb graph build'" in str(result.exception)
    assert "artefacts/graph/" in str(result.exception)


def test_an_unusable_tag_is_refused_before_anything_runs(minimal_project: Path):
    rb = RtlBuddy(name="test_run_tag")
    result = CliRunner().invoke(rb.app, ["--run-tag", "///", "test", "--list"])
    assert result.exit_code != 0
    assert "no usable characters" in str(result.exception)


# ---------------------------------------------------------------------------
# Dispatched jobs
# ---------------------------------------------------------------------------


def _spec_kwargs(tmp_path):
    return dict(
        suite_dir=str(tmp_path),
        test_config_path=str(tmp_path / "tests.yaml"),
    )


def test_a_dispatched_job_carries_the_tag_as_a_global_option(tmp_path):
    """Without it the first Slurm job writes back into the untagged tree."""
    argv = _test_job_argv(
        _TestJobSpec(
            test_name="alpha",
            result_json=tmp_path / "result.json",
            run_tag="vcs",
            **_spec_kwargs(tmp_path),
        )
    )
    assert "--run-tag" in argv
    # Global options precede the subcommand, or click never sees them.
    assert argv.index("--run-tag") < argv.index("_test-job")
    assert argv[argv.index("--run-tag") + 1] == "vcs"

    build = build_job_argv(BuildJobSpec(run_tag="vcs", **_spec_kwargs(tmp_path)))
    assert build.index("--run-tag") < build.index("_build-job")


def test_an_untagged_job_argv_is_unchanged(tmp_path):
    """A project that never asks for a tag sees no job-script diff."""
    argv = _test_job_argv(
        _TestJobSpec(
            test_name="alpha",
            result_json=tmp_path / "result.json",
            **_spec_kwargs(tmp_path),
        )
    )
    assert "--run-tag" not in argv
    assert "--run-tag" not in build_job_argv(BuildJobSpec(**_spec_kwargs(tmp_path)))


# ---------------------------------------------------------------------------
# `rb graph results`
# ---------------------------------------------------------------------------


def _write_run(suite_dir: Path, test: str, result: str, run_tag=None) -> None:
    artefacts = test_artifact_dir(suite_dir, test, run_tag=run_tag)
    artefacts.mkdir(parents=True, exist_ok=True)
    (artefacts / "result.json").write_text(
        json.dumps(
            {
                "rtl-buddy-filetype": "test_result",
                "schema_version": 1,
                "test": test,
                "run_id": None,
                "result": {"results": {"result": result}},
            }
        )
    )


def _project_with_two_runs(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    suite = root / "verif" / "blk"
    suite.mkdir(parents=True)
    (root / "root_config.yaml").write_text("cfg-rtl-builder:\n")
    (suite / "tests.yaml").write_text("tests:\n  - name: alpha\n")
    _write_run(suite, "alpha", "PASS", run_tag="vcs")
    _write_run(suite, "alpha", "FAIL", run_tag="verilator")
    return root


def test_each_tagged_run_converts_only_its_own_tree(tmp_path):
    root = _project_with_two_runs(tmp_path)

    passing = graph_results.collect_results(root, coverage=False, run_tag="vcs")
    failing = graph_results.collect_results(root, coverage=False, run_tag="verilator")

    assert [e["status"] for e in passing.entries.values()] == ["PASS"]
    assert [e["status"] for e in failing.entries.values()] == ["FAIL"]


def test_the_overlay_lands_in_the_tagged_tree_and_the_graph_is_read_from_the_flat_one(
    tmp_path,
):
    """The graph describes the design, not the run.

    Requiring `rb graph build --run-tag` per tag would be a trap, so the
    overlay moves and ``graph.json`` does not.
    """
    root = _project_with_two_runs(tmp_path)
    flat_graph = root / "artefacts" / "graph"
    flat_graph.mkdir(parents=True)
    (flat_graph / "graph.json").write_text(
        json.dumps({"rtl-buddy-filetype": "graph", "nodes": [], "edges": []})
    )

    overlay = graph_results.refresh_results_overlay(root, coverage=False, run_tag="vcs")

    assert overlay.path == (
        root / "artefacts" / ".runs" / "vcs" / "graph" / "results-overlay.json"
    )
    assert overlay.path.is_file()
    assert not (flat_graph / "results-overlay.json").exists()


# ---------------------------------------------------------------------------
# The other paths two heads would otherwise share
# ---------------------------------------------------------------------------


def test_coverage_publishes_into_the_tagged_tree(tmp_path):
    """Raw databases are per test; the published reports are not.

    `cov_dir` holds one manifest, one coverage model and one set of merged
    LCOV/HTML outputs for a whole run, so two tagged runs sharing it would
    overwrite each other's reports while their locks said they were
    isolated.
    """
    untagged = CoverageReporter(_DummyCovRootCfg(tmp_path))
    tagged = CoverageReporter(_DummyCovRootCfg(tmp_path), run_tag="vcs")

    assert Path(untagged._cov_dir(tmp_path)) == tmp_path / "cov_dir"
    assert Path(tagged._cov_dir(tmp_path)) == (
        tmp_path / "artefacts" / ".runs" / "vcs" / "cov_dir"
    )
    assert Path(tagged._cov_dir(tmp_path)) != Path(
        CoverageReporter(_DummyCovRootCfg(tmp_path), run_tag="verilator")._cov_dir(
            tmp_path
        )
    )


def test_each_tagged_head_logs_into_its_own_tree(tmp_path):
    """`attach_file_log` truncates a path on its first open in a process, so
    two heads sharing one command root would erase each other's log."""
    (tmp_path / "tests.yaml").write_text("tests:\n")

    def ctx_for(tag):
        return ExecutionContext.for_command(
            invocation_cwd=tmp_path,
            primary_config=tmp_path / "tests.yaml",
            run_tag=tag,
        )

    assert ctx_for(None).log_path == tmp_path / "rtl_buddy.log"
    assert ctx_for("vcs").log_path == (
        tmp_path / "artefacts" / ".runs" / "vcs" / "rtl_buddy.log"
    )
    assert ctx_for("vcs").log_path != ctx_for("verilator").log_path


def test_the_graph_read_verbs_resolve_the_tagged_overlay(tmp_path):
    """`graph query`, `path` and `explain` share one loader, so a tag that
    reached only `results` would have them reading another run's results."""
    root = _project_with_two_runs(tmp_path)
    graph_dir = root / "artefacts" / "graph"
    graph_dir.mkdir(parents=True)
    (graph_dir / "graph.json").write_text(
        json.dumps({"rtl-buddy-filetype": "graph", "nodes": [], "edges": []})
    )
    for tag, status in (("vcs", "PASS"), ("verilator", "FAIL")):
        graph_results.refresh_results_overlay(root, coverage=False, run_tag=tag)

    for tag in ("vcs", "verilator"):
        context = graph_query.load_context(root, run_tag=tag)
        # The graph comes from where it was built; only the overlay moves.
        assert context.graph_path == graph_dir / "graph.json"
        assert context.overlay_path == (
            root / "artefacts" / ".runs" / tag / "graph" / "results-overlay.json"
        )
        assert context.overlay is not None

    untagged = graph_query.load_context(root)
    assert untagged.overlay_path == graph_dir / "results-overlay.json"


# ---------------------------------------------------------------------------
# The thing the issue actually asked for
# ---------------------------------------------------------------------------


pytestmark_posix = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="the fake verilator this fixture builds with needs a POSIX shell",
)


@pytestmark_posix
def test_two_tagged_regressions_run_concurrently_in_one_checkout(tmp_path):
    """One checkout, two simultaneous regressions, both to PASS.

    The workaround this replaces is running them in sequence and snapshotting
    each report before the next overwrites the tree, which costs the sum of
    two runtimes for work that has no serial dependency.
    """
    project = tmp_path / "proj"
    shutil.copytree(_FIXTURE, project)
    env = dict(os.environ)
    env["PATH"] = f"{_SHIMS}{os.pathsep}{env['PATH']}"
    env["RB_SHIM_DB"] = str(tmp_path / "jobs.db")

    procs = {
        tag: subprocess.Popen(
            [
                sys.executable,
                "-m",
                "rtl_buddy",
                "--machine",
                "--run-tag",
                tag,
                "regression",
                "-c",
                "regression.yaml",
                "--dispatch",
                "local-parallel",
            ],
            cwd=project,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for tag in ("left", "right")
    }
    output = {tag: proc.communicate() for tag, proc in procs.items()}

    for tag, proc in procs.items():
        out, err = output[tag]
        assert proc.returncode == 0, f"[{tag}]\n{out}\n{err}"
        envelope = None
        for line in out.splitlines():
            if line.startswith('{"command"'):
                envelope = json.loads(line)
        assert envelope is not None, f"[{tag}]\n{out}\n{err}"
        results = {r["name"]: r["result"] for r in envelope["payload"]["results"]}
        assert results == {"alpha": "PASS", "beta": "PASS"}, f"[{tag}]\n{out}"

    artefacts = project / "verif" / "blk" / "artefacts"
    for tag in ("left", "right"):
        tree = artefacts / ".runs" / tag
        assert (tree / "alpha" / "result.json").is_file()
        assert (tree / "beta" / "result.json").is_file()
        assert list(tree.glob(".shared-builds/obj_dir_*")) != []
        assert list(tree.glob(".dispatch/build-result-*.json")) != []

    # Nothing leaked into the flat tree the two runs would otherwise share.
    assert not (artefacts / "alpha").exists()
    assert not (artefacts / "beta").exists()
    assert not (artefacts / ".shared-builds").exists()

    # Nor onto the one path outside the tree two runs would still race for:
    # the latest-run convenience links, which "latest" cannot describe when
    # two runs are live.
    suite = project / "verif" / "blk"
    assert not (suite / "test.log").exists()
    assert not (suite / "test.err").exists()
