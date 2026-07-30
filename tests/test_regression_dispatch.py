"""Dispatched regression flow tests (#351 P1).

Exercise ``rb regression --dispatch ...`` end-to-end over the
``minimal_project`` fixture with a fake backend and a stubbed
``TestRunner``: head-node build pass, fan-out, collection, failure
mapping, and result ordering — no scheduler or simulator involved.
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
        self.waited = False
        self.cancelled = False

    def submit(self, spec):
        self.submitted.append(spec)
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
    stub_build_runner: type[_StubBuildRunner],
    fake_backend: _FakeBackend,
):
    result, rb = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    # Only "basic" (reglvl 0) runs at -l 0; "extra" (reglvl 5) is skipped.
    assert [spec.test_name for spec in fake_backend.submitted] == ["basic"]
    assert fake_backend.waited
    assert not fake_backend.cancelled

    # The head build pass ran with share_build + COMP early-stop.
    build_kwargs = stub_build_runner.inits[0]
    assert build_kwargs["share_build"] is True
    assert build_kwargs["run_depth"].value == "comp"
    # Dispatch implies share_build for the command as a whole.
    assert rb.share_build is True

    # Jobs carry resolved resources with an always-defined time limit.
    spec = fake_backend.submitted[0]
    assert spec.resources.time is not None
    assert spec.result_json.is_file()


def test_dispatched_regression_missing_result_is_dispatch_fail(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
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


def test_dispatched_regression_compile_fail_submits_nothing(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    fake_backend: _FakeBackend,
):
    stub_build_runner.canned = CompileFailResults(name="build/results")
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 1
    assert fake_backend.submitted == []
    assert not fake_backend.waited or fake_backend.submitted == []


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
        def submit(self, spec):
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
        def submit(self, spec):
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
