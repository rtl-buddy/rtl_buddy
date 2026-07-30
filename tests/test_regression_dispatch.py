"""Dispatched regression flow tests (#351 P1).

Exercise ``rb regression --dispatch ...`` end-to-end over the
``minimal_project`` fixture with a fake backend: dispatched build job,
sim fan-out gated on it, collection, failure mapping, and result
ordering — no scheduler or simulator involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import rtl_buddy.rtl_buddy as rtl_buddy_module
from rtl_buddy.dispatch.base import DispatchBackend, JobHandle
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.result_io import write_result_json
from rtl_buddy.runner.test_results import (
    CompileFailResults,
    EarlyStopResults,
    TestPassResults,
)


class _FakeBackend(DispatchBackend):
    """Records specs; 'runs' each job at submit time by writing (or
    deliberately not writing) its result envelope."""

    name = "fake"

    def __init__(self, job_result="PASS", write_results=True):
        self.job_result = job_result
        self.write_results = write_results
        self.submitted = []
        self.build_submitted = []
        self.dependencies = []
        self.waited = False
        self.cancelled = False

    def submit_build(self, spec):
        self.build_submitted.append(spec)
        return JobHandle(job_id="fake-build", spec=spec)

    def submit(self, spec, *, dependency=None):
        self.submitted.append(spec)
        self.dependencies.append(dependency)
        if self.write_results:
            results = (
                TestPassResults(name=spec.test_name + "/results")
                if self.job_result == "PASS"
                else CompileFailResults(name=spec.test_name + "/results")
            )
            write_result_json(
                spec.result_json,
                test_name=spec.test_name,
                run_id=spec.run_id,
                results=results,
            )
        return JobHandle(job_id=f"fake-{len(self.submitted)}", spec=spec)

    def wait_all(self, handles):
        self.waited = True

    def cancel_all(self, handles):
        self.cancelled = True


class _StubBuildRunner:
    """TestRunner stand-in for the head-node build pass."""

    canned = None
    inits = []

    def __init__(self, **kwargs):
        type(self).inits.append(kwargs)

    def run(self):
        return type(self).canned

    def run_multiple(self, run_ids):
        return [type(self).canned for _ in run_ids]


@pytest.fixture
def stub_build_runner(monkeypatch: pytest.MonkeyPatch) -> type[_StubBuildRunner]:
    _StubBuildRunner.canned = EarlyStopResults(
        name="build/results", desc="Stopped early at compile"
    )
    _StubBuildRunner.inits = []
    monkeypatch.setattr(rtl_buddy_module, "TestRunner", _StubBuildRunner)
    return _StubBuildRunner


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> _FakeBackend:
    backend = _FakeBackend()
    monkeypatch.setattr(
        rtl_buddy_module,
        "create_dispatch_backend",
        lambda name, cfg: backend if name not in (None, "local") else None,
    )
    return backend


def _invoke(args):
    runner, rb = CliRunner(), RtlBuddy(name="test_regression_dispatch")
    return runner.invoke(rb.app, args), rb


def test_dispatched_regression_passes(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    result, rb = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    # Only "basic" (reglvl 0) runs at -l 0; "extra" (reglvl 5) is skipped.
    assert [spec.test_name for spec in fake_backend.submitted] == ["basic"]
    assert fake_backend.waited
    assert not fake_backend.cancelled

    # The compile runs as a dispatched build job (never on the head), and
    # the sim job is gated on it via afterok.
    assert len(fake_backend.build_submitted) == 1
    build = fake_backend.build_submitted[0]
    assert build.resources.time is not None  # compile reservation resolved
    assert fake_backend.dependencies == ["fake-build"]

    # Dispatch implies share_build; sim jobs carry a defined reservation.
    assert rb.share_build is True
    spec = fake_backend.submitted[0]
    assert spec.share_build is True
    assert spec.resources.time is not None
    assert spec.result_json.is_file()


def test_dispatched_regression_missing_result_is_dispatch_fail(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    fake_backend.write_results = False
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 1, result.output

    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    envelope = json.loads(payload_line)
    rows = {r["name"]: r for r in envelope["payload"]["results"]}
    assert rows["basic"]["result"] == "FAIL"
    assert "produced no result" in rows["basic"]["desc"]


def test_dispatched_regression_submits_build_before_sims(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    # A build job is always submitted (the compile no longer runs on the
    # head), and every sim depends on it. Compile failures now surface via
    # the sim job's own envelope, not by the head refusing to submit.
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    assert len(fake_backend.build_submitted) == 1
    assert fake_backend.submitted, "expected sim jobs submitted"
    assert all(dep == "fake-build" for dep in fake_backend.dependencies)


def test_dispatch_local_keeps_in_process_path(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    fake_backend: _FakeBackend,
):
    stub_build_runner.canned = TestPassResults(name="basic/results")
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "local"])
    assert result.exit_code == 0, result.output
    # No jobs — the stubbed TestRunner ran in-process via _do_test_suite.
    assert fake_backend.submitted == []


def test_dispatch_unknown_backend_fails_loud(minimal_project: Path):
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "--dispatch", "nonsense"]
    )
    assert result.exit_code != 0


def test_cfg_dispatch_backend_used_when_no_cli_flag(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    fake_backend: _FakeBackend,
):
    root_cfg_path = minimal_project / "root_config.yaml"
    root_cfg_path.write_text(
        root_cfg_path.read_text() + "\ncfg-dispatch:\n  backend: slurm\n"
    )
    result, _ = _invoke(["regression", "-c", "regression.yaml"])
    assert result.exit_code == 0, result.output
    assert [spec.test_name for spec in fake_backend.submitted] == ["basic"]


# --------------------------------------------- P1 review: robustness fixes


def test_dispatch_creates_log_parent_before_submit(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    # Regression for the blocking bug: the sbatch --output parent must
    # exist at submit time (slurmstepd opens it before rb _test-job runs).
    seen_parent_exists = []

    class _CheckBackend(_FakeBackend):
        def submit(self, spec, *, dependency=None):
            seen_parent_exists.append(spec.log_path.parent.is_dir())
            return super().submit(spec)

    backend = _CheckBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", lambda name, cfg: backend
    )
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    assert seen_parent_exists == [True]


def test_dispatch_cancels_already_submitted_on_midway_submit_failure(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    # Two tests at -l 5; the second submit raises. The first must be
    # cancelled, not left running after the head exits.
    from rtl_buddy.errors import FatalRtlBuddyError

    class _FlakyBackend(_FakeBackend):
        def submit(self, spec, *, dependency=None):
            if len(self.submitted) >= 1:
                raise FatalRtlBuddyError("sbatch: QOS limit reached")
            return super().submit(spec)

    backend = _FlakyBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", lambda name, cfg: backend
    )
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code != 0
    assert backend.cancelled is True


def test_build_compile_failure_surfaces_as_compile_fail(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # The build job records "basic" as a compile failure; its sim job's
    # recompile is then killed (writes no envelope). The head must map that
    # to a CompileFail — the clean design-error result the in-process path
    # produces — not an infrastructure DispatchFail.
    from rtl_buddy.runner.result_io import write_build_result_json

    class _CompileFailBuild(_FakeBackend):
        def __init__(self):
            super().__init__(write_results=False)  # sim envelope never appears

        def submit_build(self, spec):
            write_build_result_json(spec.result_json, built=[], failed=["basic"])
            return super().submit_build(spec)

    backend = _CompileFailBuild()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", lambda name, cfg: backend
    )
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 1, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    rows = {r["name"]: r for r in json.loads(payload_line)["payload"]["results"]}
    assert rows["basic"]["result"] == "FAIL"
    assert "compile failed in build job" in rows["basic"]["desc"]
    assert "produced no result" not in rows["basic"]["desc"]


def test_empty_suite_submits_no_build_job(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    # Every test filtered out by the level window: no compile, no jobs, no
    # build job queued for zero work (which wait_all would then block on).
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-s", "100", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    assert fake_backend.build_submitted == []
    assert fake_backend.submitted == []


def test_dispatch_writes_plan_and_threads_it_to_jobs(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    # The head writes one plan manifest and hands it to both the build job
    # and every sim job (so neither re-runs the sweep hook).
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert build.plan_path is not None and Path(build.plan_path).is_file()
    sim = fake_backend.submitted[0]
    assert sim.plan_path == build.plan_path


def test_early_stop_with_dispatch_rejected(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    fake_backend: _FakeBackend,
):
    result, _ = _invoke(
        [
            "--early-stop",
            "comp",
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "slurm",
        ]
    )
    assert result.exit_code != 0
    assert fake_backend.submitted == []
