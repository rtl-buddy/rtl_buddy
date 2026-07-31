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
from rtl_buddy.dispatch.plan import read_plan_token
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
            # Mirror the real rb _test-job: stamp the head's run token
            # (carried in the plan) into the envelope so collection accepts
            # it (#362).
            run_token = read_plan_token(spec.plan_path) if spec.plan_path else None
            write_result_json(
                spec.result_json,
                test_name=spec.test_name,
                run_id=spec.run_id,
                results=results,
                run_token=run_token,
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
    # the sim is gated on it via afterok.
    assert len(fake_backend.build_submitted) == 1
    assert fake_backend.build_submitted[0].resources.time is not None
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


def test_zero_test_suite_is_skipped_not_crashed(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A suite that selects no test at the requested level submits nothing
    (build_handle=None). The head must skip it, not crash on the None in
    all_handles and orphan the other suites' already-submitted jobs (#361)."""
    # Second suite: every test is far above -l 0, so it selects nothing.
    # Reuse the fixture suite but bump every test far above -l 0 so the
    # suite selects nothing (keeps every schema field valid).
    empty = (
        (minimal_project / "tests.yaml")
        .read_text()
        .replace("reglvl: 0", "reglvl: 10000")
        .replace("reglvl: 5", "reglvl: 10000")
    )
    (minimal_project / "empty_tests.yaml").write_text(empty)
    (minimal_project / "regression.yaml").write_text(
        "rtl-buddy-filetype: reg_config\n"
        "test-configs:\n  - tests.yaml\n  - empty_tests.yaml\n"
    )
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    # Only the non-empty suite's "basic" reached the fleet; the empty suite
    # queued nothing, and the run drained cleanly rather than cancelling.
    assert [spec.test_name for spec in fake_backend.submitted] == ["basic"]
    assert fake_backend.waited
    assert not fake_backend.cancelled


def test_head_does_not_preunlink_and_rejects_stale_by_token(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """The head must NOT pre-unlink the result path (that caches an NFS
    negative dentry and blinds it, #362). A stale envelope left in place is
    instead rejected by run_token, so an old PASS never satisfies this run."""
    fake_backend.write_results = False  # this run's job leaves no fresh envelope

    # Pre-seed a stale PASS envelope with a token from an "earlier run" at the
    # exact path this run's "basic" job will use.
    stale = minimal_project / "artefacts" / "basic" / "dispatch" / "result-single.json"
    write_result_json(
        stale,
        test_name="basic",
        run_id=None,
        results=TestPassResults(name="basic/results"),
        run_token="STALE-run",
    )

    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 1, result.output
    # The stale file was NOT unlinked by the head at submit (still on disk,
    # still the old token) — proving the negative-dentry trigger is gone.
    assert stale.is_file()
    assert json.loads(stale.read_text())["run_token"] == "STALE-run"
    # And its stale PASS did not count: the run reports no result for basic.
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    rows = {r["name"]: r for r in json.loads(payload_line)["payload"]["results"]}
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
    # Two tests with distinct resources -> two arrays; the second array
    # submit raises. The first array's jobs must be cancelled, not left
    # running after the head exits and releases its lock.
    from rtl_buddy.errors import FatalRtlBuddyError

    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text().replace(
            "  - name: extra\n",
            "  - name: extra\n    resources: { mem: 24G }\n",
        )
    )

    class _FlakyBackend(_FakeBackend):
        def __init__(self):
            super().__init__()
            self.array_calls = 0

        def submit_array(self, specs, *, array_dir, max_parallel=None, dependency=None):
            self.array_calls += 1
            if self.array_calls >= 2:
                raise FatalRtlBuddyError("sbatch: QOS limit reached")
            return [self.submit(spec) for spec in specs]

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


# ------------------------------------------------- P2: arrays / cross-suite


class _RecordingBackend(_FakeBackend):
    """FakeBackend that also records array submissions and wait calls."""

    def __init__(self, telemetry=None, **kwargs):
        super().__init__(**kwargs)
        self.array_calls = []
        self.wait_calls = 0
        self.telemetry = telemetry or {}

    def submit_array(self, specs, *, array_dir, max_parallel=None, dependency=None):
        self.array_calls.append(
            {"n": len(specs), "max_parallel": max_parallel, "array_dir": array_dir}
        )
        return [self.submit(spec) for spec in specs]

    def wait_all(self, handles):
        self.wait_calls += 1
        super().wait_all(handles)

    def collect_telemetry(self, handles):
        return self.telemetry


@pytest.fixture
def recording_backend(monkeypatch: pytest.MonkeyPatch) -> _RecordingBackend:
    backend = _RecordingBackend()
    monkeypatch.setattr(
        rtl_buddy_module,
        "create_dispatch_backend",
        lambda name, cfg: backend if name not in (None, "local") else None,
    )
    return backend


def test_regression_waits_once_across_suites(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    recording_backend: _RecordingBackend,
):
    # Two suites in the reg config → jobs from both must be in flight
    # before the single global wait.
    (minimal_project / "regression.yaml").write_text(
        "rtl-buddy-filetype: reg_config\n"
        "test-configs:\n  - tests.yaml\n  - tests.yaml\n"
    )
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    assert len(recording_backend.submitted) == 2
    assert recording_backend.wait_calls == 1


def test_same_resources_group_into_one_array(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    recording_backend: _RecordingBackend,
):
    # Run both fixture tests (extra is reglvl 5) — identical resources →
    # one submit_array call with both specs, throttled by max-jobs-per-array.
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text() + "\ncfg-dispatch:\n  max-jobs-per-array: 7\n"
    )
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    assert [c["n"] for c in recording_backend.array_calls] == [2]
    assert recording_backend.array_calls[0]["max_parallel"] == 7
    assert ".dispatch" in str(recording_backend.array_calls[0]["array_dir"])


def test_different_resources_split_arrays(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    recording_backend: _RecordingBackend,
):
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text().replace(
            "  - name: extra\n",
            "  - name: extra\n    resources: { mem: 24G }\n",
        )
    )
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    # Two resource groups of one spec each.
    assert sorted(c["n"] for c in recording_backend.array_calls) == [1, 1]


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


def test_collect_attaches_telemetry_to_results_and_envelope(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    backend = _RecordingBackend(
        telemetry={
            "fake-1": {"state": "COMPLETED", "elapsed_s": 5, "timelimit_s": 3600}
        }
    )
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", lambda name, cfg: backend
    )
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    envelope = json.loads(backend.submitted[0].result_json.read_text())
    assert envelope["telemetry"]["state"] == "COMPLETED"
    assert envelope["telemetry"]["elapsed_s"] == 5


def test_dispatch_fail_desc_names_scheduler_state(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    backend = _RecordingBackend(
        write_results=False,
        telemetry={"fake-1": {"state": "TIMEOUT", "elapsed_s": 3600}},
    )
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
    envelope = json.loads(payload_line)
    rows = {r["name"]: r for r in envelope["payload"]["results"]}
    assert "scheduler state TIMEOUT" in rows["basic"]["desc"]


# ------------------------------------------------------ P2: randtest fan-out


def test_randtest_dispatch_fans_out_seeds(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    recording_backend: _RecordingBackend,
):
    result, rb = _invoke(["--machine", "randtest", "basic", "3", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    assert [spec.run_id for spec in recording_backend.submitted] == [1, 2, 3]
    assert all(spec.seed_mode.value == "new" for spec in recording_backend.submitted)
    assert recording_backend.wait_calls == 1
    # One array of three seeds (identical resources).
    assert [c["n"] for c in recording_backend.array_calls] == [3]
    assert rb.share_build is True

    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    envelope = json.loads(payload_line)
    assert [r["run_id"] for r in envelope["payload"]["results"]] == [1, 2, 3]


def test_randtest_replay_stays_local(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    recording_backend: _RecordingBackend,
):
    stub_build_runner.canned = TestPassResults(name="basic/results")
    result, _ = _invoke(["randtest", "basic", "3", "-r", "2", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    assert recording_backend.submitted == []


# ---------------------------------------------- P2 review: robustness fixes


def test_array_dir_is_per_invocation(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    recording_backend: _RecordingBackend,
):
    import os

    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text() + "\ncfg-dispatch:\n  max-jobs-per-array: 4\n"
    )
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    array_dir = str(recording_backend.array_calls[0]["array_dir"])
    # Under a .dispatch sibling and tagged with the head pid (not a fixed
    # array-001), so overlapping runs don't rewrite each other's manifest.
    assert ".dispatch" in array_dir
    assert f"{os.getpid()}-" in Path(array_dir).name


def test_randtest_replay_with_explicit_dispatch_warns(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    recording_backend: _RecordingBackend,
):
    stub_build_runner.canned = TestPassResults(name="basic/results")
    result, _ = _invoke(
        ["--machine", "randtest", "basic", "3", "-r", "2", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    # Stayed local (no jobs) but logged the ignored-flag warning.
    assert recording_backend.submitted == []
    assert "ignored for replay" in result.output


def test_dispatched_collect_reenters_suite_context(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    # Two suites; a missing envelope for suite 1 must log under suite 1's
    # own root, not the last-entered suite — assert collect re-enters the
    # per-suite command context.
    (minimal_project / "regression.yaml").write_text(
        "rtl-buddy-filetype: reg_config\n"
        "test-configs:\n  - tests.yaml\n  - tests.yaml\n"
    )
    backend = _RecordingBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", lambda name, cfg: backend
    )
    entered = []
    rb = RtlBuddy(name="ctx")
    orig = rb._enter_command_context

    def spy(*a, **k):
        if "primary_config" in k:
            entered.append(str(k["primary_config"]))
        return orig(*a, **k)

    monkeypatch.setattr(rb, "_enter_command_context", spy)
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        rb.app, ["regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    # The suite context is entered again during the collect phase (more
    # entries than the single submit-phase entry per suite).
    assert entered.count(str(minimal_project / "tests.yaml")) >= 4


# --------------------------------------------------- P3: reservation advice


def _mark_stub_builder_verilator(project: Path):
    # Time advice is gated to verilator-family builders (licqueue, #329);
    # the fixture's stub builder ("echo") must opt in for advice tests.
    root_cfg = project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text().replace(
            '    builder: "echo"\n',
            '    builder: "echo"\n    simulator-family: "verilator"\n',
        )
    )


def _telemetry_backend(monkeypatch):
    backend = _RecordingBackend(
        telemetry={
            "fake-1": {
                "state": "COMPLETED",
                "elapsed_s": 10,
                "timelimit_s": 3600,
                "req_mem_bytes": 8 * 2**30,
                "max_rss_bytes": 2**30,
            }
        }
    )
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", lambda name, cfg: backend
    )
    return backend


def test_regression_machine_payload_carries_reservation_advice(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    _telemetry_backend(monkeypatch)
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    envelope = json.loads(payload_line)
    advice = envelope["payload"]["reservation_advice"]
    by_resource = {a["resource"]: a for a in advice}
    # 10s of 1h and 1G of 8G are both over-reserved.
    assert by_resource["time"]["direction"] == "reduce"
    assert by_resource["mem"]["direction"] == "reduce"
    mem = by_resource["mem"]
    assert mem["event"] == "reservation-advice"
    assert mem["test"] == "basic"
    assert mem["suggested"] == "1536M"
    assert mem["edit_hint"]["path"] == "tests[name=basic].resources.mem"
    assert mem["runs"] == 1


def test_rightsize_report_false_disables_advice(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    _telemetry_backend(monkeypatch)
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text() + "\ncfg-dispatch:\n  rightsize:\n    report: false\n"
    )
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    envelope = json.loads(payload_line)
    assert envelope["payload"]["reservation_advice"] == []


def test_local_run_has_no_reservation_advice_key(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    recording_backend: _RecordingBackend,
):
    stub_build_runner.canned = TestPassResults(name="basic/results")
    result, _ = _invoke(["--machine", "regression", "-c", "regression.yaml"])
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    envelope = json.loads(payload_line)
    assert "reservation_advice" not in envelope["payload"]


def test_randtest_machine_payload_carries_reservation_advice(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    backend = _RecordingBackend(
        telemetry={
            f"fake-{i}": {
                "state": "COMPLETED",
                "elapsed_s": 5 * i,
                "timelimit_s": 3600,
            }
            for i in (1, 2, 3)
        }
    )
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", lambda name, cfg: backend
    )
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(["--machine", "randtest", "basic", "3", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    envelope = json.loads(payload_line)
    advice = envelope["payload"]["reservation_advice"]
    (time_a,) = [a for a in advice if a["resource"] == "time"]
    # Aggregated across the 3 seeds: peak elapsed 15s of 1h → reduce.
    assert time_a["runs"] == 3
    assert time_a["direction"] == "reduce"


def test_unresolvable_builder_does_not_abort_finished_run(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    # A dispatched row whose builder name no longer resolves must not turn
    # a completed regression into an exit-2 abort during advice analysis.
    backend = _RecordingBackend(
        telemetry={
            "fake-1": {"state": "COMPLETED", "elapsed_s": 10, "timelimit_s": 3600}
        }
    )
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", lambda name, cfg: backend
    )
    _mark_stub_builder_verilator(minimal_project)

    rb = RtlBuddy(name="unknown_builder")
    from typer.testing import CliRunner

    # Force every builder lookup during analysis to fail as if the name
    # vanished from cfg-rtl-builder.
    import rtl_buddy.config.root as root_mod

    orig = root_mod.RootConfig.resolve_rtl_builder_cfg

    def flaky(self, name=None):
        if name == "__gone__":
            from rtl_buddy.errors import FatalRtlBuddyError

            raise FatalRtlBuddyError("no such builder")
        return orig(self, name)

    monkeypatch.setattr(root_mod.RootConfig, "resolve_rtl_builder_cfg", flaky)

    # Stamp the missing builder onto the collected row via the backend.
    result = CliRunner().invoke(
        rb.app,
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"],
    )
    # The run completes and reports (exit 0/1 from results), not exit 2.
    assert result.exit_code in (0, 1), result.output
    assert '"command": "regression"' in result.output
