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
from rtl_buddy.errors import FatalRtlBuddyError
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


def _set_stub_builder_family(project: Path, family: str):
    """Declare a simulator family on the fixture's stub builder.

    The fixture's `builder: "echo"` infers the family `"echo"`, which is
    neither share-build capable (so no build job is submitted, #358) nor
    eligible for time advice (gated to verilator, #329). Tests that exercise
    either path have to say which family they mean.
    """
    root_cfg = project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text().replace(
            '    builder: "echo"\n',
            f'    builder: "echo"\n    simulator-family: "{family}"\n',
        )
    )


def _mark_stub_builder_verilator(project: Path):
    _set_stub_builder_family(project, "verilator")


def test_dispatched_regression_passes(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    _mark_stub_builder_verilator(minimal_project)
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
    # With a share-build-capable builder a build job is submitted (the
    # compile no longer runs on the head), and every sim depends on it.
    # Compile failures now surface via the sim job's own envelope, not by
    # the head refusing to submit.
    _mark_stub_builder_verilator(minimal_project)
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

    _mark_stub_builder_verilator(minimal_project)

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
    _mark_stub_builder_verilator(minimal_project)
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
            {
                "n": len(specs),
                "max_parallel": max_parallel,
                "array_dir": array_dir,
                "dependency": dependency,
            }
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


# ------------------------------------ #358: builders that compile in-job


def _add_dispatch_resources(project: Path, block: str):
    root_cfg = project / "root_config.yaml"
    root_cfg.write_text(root_cfg.read_text() + block)


def test_no_build_job_when_no_test_can_share_a_build(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A build job whose output no sim job can read is pure waste (#358).

    The fixture's inferred "echo" family has no shared-build support, so the
    head must skip the build pass entirely rather than burn a compile on a
    compute node and make every element queue behind it.
    """
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    assert fake_backend.build_submitted == []
    assert [spec.test_name for spec in fake_backend.submitted] == ["basic"]
    # Nothing to gate on: the element compiles for itself and runs unblocked.
    assert fake_backend.dependencies == [None]


def test_in_job_compile_reservation_covers_both_phases(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """The one allocation is sized max(sim, compile) field by field (#358)."""
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "00:20:00"\n'
        '  compile:\n    cpus: 8\n    mem: 16G\n    time: "00:10:00"\n',
    )
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    resources = fake_backend.submitted[0].resources
    assert resources.cpus == 8  # compile needs more
    assert resources.mem == "16G"  # compile needs more
    assert resources.time == "00:20:00"  # sim needs more; compile's is smaller


def test_share_build_capable_builder_keeps_the_sim_sized_reservation(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """The compile block must NOT inflate sim jobs that only simulate."""
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "00:20:00"\n'
        '  compile:\n    cpus: 8\n    mem: 16G\n    time: "02:00:00"\n',
    )
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    sim = fake_backend.submitted[0].resources
    assert (sim.cpus, sim.mem, sim.time) == (1, "2G", "00:20:00")
    # ...while the build job it depends on carries the compile reservation.
    build = fake_backend.build_submitted[0].resources
    assert (build.cpus, build.mem, build.time) == (8, "16G", "02:00:00")


def test_fanned_out_in_job_compile_gets_a_build_job_to_serialize_it(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    fake_backend: _FakeBackend,
):
    """The reported defect (#369): a dispatched randtest on a builder with no
    shared-build support ran N full compiles into one `artefacts/<test>/` at
    once, and the losers reported `Compile failed` with nothing wrong.

    The fix is a single writer — the build job compiles once and every
    element waits for it, then short-circuits on the stamp it left.
    """
    result, _ = _invoke(["randtest", "basic", "3", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    assert len(fake_backend.build_submitted) == 1
    assert [spec.run_id for spec in fake_backend.submitted] == [1, 2, 3]
    # No element starts before the compile it would otherwise have raced.
    assert fake_backend.dependencies == ["fake-build"] * 3


def test_single_run_in_job_compiles_still_skip_the_build_job(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """One writer per directory already: distinct tests each own their own
    `artefacts/<test>/`, so serializing them behind a build job would only
    trade parallel compiles for serial ones."""
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    assert fake_backend.build_submitted == []
    assert [spec.test_name for spec in fake_backend.submitted] == ["basic", "extra"]
    assert fake_backend.dependencies == [None, None]


def _add_second_builder(project: Path, *, name: str, family: str):
    """Give the fixture a second builder so a suite can mix families."""
    root_cfg = project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text().replace(
            "cfg-verible:",
            f'  - name: "{name}"\n'
            f'    builder: "echo"\n'
            f'    simulator-family: "{family}"\n'
            f'    builder-simv: "obj_dir/simv"\n'
            f"    sim-rand-seed: 1\n"
            f'    sim-rand-seed-prefix: "+seed="\n'
            f"    builder-opts:\n"
            f"      debug:\n"
            f'        compile-time: "--no-op"\n'
            f'        run-time: "--no-op"\n'
            f"\ncfg-verible:",
        )
    )


def test_every_group_waits_for_the_build_job_that_writes_its_directory(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    recording_backend: _RecordingBackend,
):
    """The build job runs PRE+COMPILE for the whole plan, so it writes into a
    self-compiling test's artefact dir too — an ungated element would be the
    second writer there. That is the mixed-builder case, which needs no
    fan-out at all: one shareable test puts a build job in the plan, and the
    unshareable one beside it must still wait for it.
    """
    _mark_stub_builder_verilator(minimal_project)
    _add_second_builder(minimal_project, name="unshareable", family="questa")
    tests_yaml = minimal_project / "tests.yaml"
    # `extra` compiles for itself AND resolves to a different reservation, so
    # the two tests land in different arrays.
    tests_yaml.write_text(
        tests_yaml.read_text().replace(
            "  - name: extra\n",
            "  - name: extra\n    builder: unshareable\n    resources: { mem: 24G }\n",
        )
    )

    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    assert [spec.test_name for spec in recording_backend.submitted] == [
        "basic",
        "extra",
    ]
    # Two reservation groups, one build job, and neither group runs unblocked.
    assert len(recording_backend.build_submitted) == 1
    assert len(recording_backend.array_calls) == 2
    assert [call["dependency"] for call in recording_backend.array_calls] == [
        "fake-build",
        "fake-build",
    ]


# --------------------------------------------------- P3: reservation advice


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


def test_advice_for_an_in_job_compile_is_clamped_to_the_compile_floor(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """End to end: advice for a job that compiled must be reachable (#358).

    The allocation is max(sim, compile), so no reduce can take it below the
    compile side. A suggestion under that floor is clamped up to it and
    re-attributed to the field that governs; one the clamp pushes all the way
    back to the current reservation saves nothing and is dropped.
    """
    _telemetry_backend(monkeypatch)  # elapsed 10s of 1h, 1G of 8G reserved
    # No simulator-family override: the fixture's inferred "echo" family has
    # no shared-build support, so the sim job compiles for itself.
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "01:00:00"\n'
        '  compile:\n    cpus: 1\n    mem: 8G\n    time: "00:30:00"\n',
    )
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]

    # time: 10s of 1h would suggest the 5-minute floor, but the compile needs
    # 30 minutes — so that is the suggestion, and cfg-dispatch.compile.time is
    # the field that would have to move.
    (time_a,) = [a for a in advice if a["resource"] == "time"]
    assert time_a["phase"] == "compile+sim"
    assert time_a["direction"] == "reduce"
    assert time_a["suggested"] == "00:30:00"
    assert time_a["edit_hint"]["path"] == "cfg-dispatch.compile.time"
    assert time_a["edit_hint"]["file"].endswith("root_config.yaml")

    # mem: reserved 8G IS the compile reservation, so every reduce clamps
    # straight back to it. Silence beats advice that cannot retire.
    assert [a for a in advice if a["resource"] == "mem"] == []


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


def test_jobs_flag_sizes_the_local_parallel_pool(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """``-j`` reaches the backend as its pool size (#360)."""
    backend, seen = _FakeBackend(), {}

    def _capture(name, cfg):
        seen["name"], seen["jobs"] = name, cfg.jobs
        return backend

    monkeypatch.setattr(rtl_buddy_module, "create_dispatch_backend", _capture)
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(
        [
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "local-parallel",
            "-j",
            "3",
        ]
    )
    assert result.exit_code == 0, result.output
    assert seen == {"name": "local-parallel", "jobs": 3}


def test_jobs_flag_is_rejected_against_a_backend_without_a_pool(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A silently ignored concurrency knob is worse than a refusal (#360)."""
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "--dispatch", "slurm", "-j", "4"]
    )
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "max-jobs-per-array" in str(result.exception)
    # Rejected before anything was submitted, so there is no fleet to clean up.
    assert not fake_backend.submitted
    assert not fake_backend.build_submitted


def test_zero_jobs_is_rejected(minimal_project: Path):
    result, _ = _invoke(
        [
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "local-parallel",
            "-j",
            "0",
        ]
    )
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "--jobs must be >= 1" in str(result.exception)


def test_missing_result_does_not_blame_a_scheduler_off_slurm(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """An unscheduled backend's failures must not be explained by `afterok`.

    The pool *is* the scheduler (#360), so "the queue killed it" is never
    the cause; the diagnostic has to name what can actually have happened.
    """

    class _PoolLikeBackend(_FakeBackend):
        name = "local-parallel"
        scheduled = False

    backend = _PoolLikeBackend(write_results=False)
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", lambda name, cfg: backend
    )
    result, _ = _invoke(
        [
            "--machine",
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "local-parallel",
        ]
    )
    assert result.exit_code == 1, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    desc = json.loads(payload_line)["payload"]["results"][0]["desc"]
    assert "produced no result" in desc
    assert "afterok" not in desc
    assert "scheduler" not in desc
    assert "never ran" in desc


def test_jobs_on_a_randtest_replay_is_never_silently_dropped(minimal_project: Path):
    """`-r` skips dispatch entirely, so `-j` has to be accounted for (#360).

    The replay path never builds a backend, so validation has to run before
    that branch; when the pool is the configured backend the flag is legal but
    unused, and the existing ignored-flag warning must name it.
    """
    # Legal but unused: warned about, alongside the ignored backend.
    result, _ = _invoke(
        ["randtest", "basic", "3", "-r", "1", "--dispatch", "local-parallel", "-j", "4"]
    )
    warned = " ".join(result.output.split())
    assert "--dispatch local-parallel (and --jobs 4) ignored for replay" in warned


def test_jobs_on_a_replay_against_a_poolless_backend_is_still_rejected(
    minimal_project: Path,
):
    """Validation runs before the replay short-circuit, so it still fires."""
    result, _ = _invoke(
        ["randtest", "basic", "3", "-r", "1", "--dispatch", "slurm", "-j", "4"]
    )
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "max-jobs-per-array" in str(result.exception)


def test_gated_jobs_are_told_they_were_gated(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    fake_backend: _FakeBackend,
):
    """A gated element that still compiles is the signal that the build's
    stamp failed and every sibling is compiling too (#369 review)."""
    result, _ = _invoke(["randtest", "basic", "3", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    assert len(fake_backend.build_submitted) == 1
    assert all(spec.expect_prebuilt for spec in fake_backend.submitted)


def test_ungated_jobs_are_not(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """With no build job there is nothing to have been prebuilt, and one
    writer per directory, so compiling is the expected path."""
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    assert fake_backend.build_submitted == []
    assert not any(spec.expect_prebuilt for spec in fake_backend.submitted)


def test_a_pinned_builder_simv_is_planned_as_compiling_in_job(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """The planner and the runtime must agree on what can share a build.

    A VCS builder with an absolute `builder-simv:` declines sharing at
    runtime; a planner consulting only the family would give it a sim-sized
    reservation and never count it as needing serialization (#369 review).
    """
    _set_stub_builder_family(minimal_project, "vcs")
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text().replace(
            '    builder-simv: "obj_dir/simv"', '    builder-simv: "/pinned/simv"'
        )
    )
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "00:20:00"\n'
        '  compile:\n    cpus: 8\n    mem: 16G\n    time: "00:10:00"\n',
    )

    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    # Sized for the compile it will really do, not for the sim alone.
    resources = fake_backend.submitted[0].resources
    assert (resources.cpus, resources.mem) == (8, "16G")
    # ...and nothing can share, so no build job is submitted for one run.
    assert fake_backend.build_submitted == []


# ------------------------- #435: the job ids reach the console before the wait


def _spy_on_console_events(monkeypatch, order):
    """Record every log_console_event the head makes, in call order."""
    real = rtl_buddy_module.log_console_event

    def spy(logger, level, event, **fields):
        order.append((event, fields))
        return real(logger, level, event, **fields)

    monkeypatch.setattr(rtl_buddy_module, "log_console_event", spy)


def _spy_on_wait(monkeypatch, backend, order):
    original = backend.wait_all

    def wait(handles):
        order.append(("wait_all", {"handles": list(handles)}))
        return original(handles)

    monkeypatch.setattr(backend, "wait_all", wait)


def test_suite_job_ids_are_announced_before_the_wait(
    minimal_project: Path,
    fake_backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
):
    """If the head dies mid-wait, those ids are the only post-mortem route.

    So the ordering is the deliverable: the line must be on the console
    *before* wait_all blocks, not reconstructed afterwards (#435).
    """
    order = []
    _spy_on_console_events(monkeypatch, order)
    _spy_on_wait(monkeypatch, fake_backend, order)
    _mark_stub_builder_verilator(minimal_project)

    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    names = [event for event, _ in order]
    assert names.index("dispatch.suite_submitted") < names.index("wait_all")
    (fields,) = [f for event, f in order if event == "dispatch.suite_submitted"]
    assert fields["job_ids"] == ["fake-1"]
    assert fields["build_job"] == "fake-build"
    # Build job counted: same scale as dispatch.progress / suite_drained.
    assert fields["jobs"] == 2
    assert fields["suite"] == "tests.yaml"
    # ...and it really reached the console at default verbosity.
    printed = " ".join(result.output.split())
    assert "dispatch: tests.yaml → build job fake-build, sim jobs fake-1" in printed


def test_a_zero_test_suite_announces_nothing(
    minimal_project: Path,
    fake_backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
):
    """Nothing was queued, so there are no ids and no wait to explain."""
    order = []
    _spy_on_console_events(monkeypatch, order)
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-s", "100", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    assert [event for event, _ in order if event == "dispatch.suite_submitted"] == []


def test_randtest_announces_its_seed_fanout_before_waiting(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    recording_backend: _RecordingBackend,
    monkeypatch: pytest.MonkeyPatch,
):
    order = []
    _spy_on_console_events(monkeypatch, order)
    _spy_on_wait(monkeypatch, recording_backend, order)

    result, _ = _invoke(["randtest", "basic", "3", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    names = [event for event, _ in order]
    assert names.index("dispatch.suite_submitted") < names.index("wait_all")
    (fields,) = [f for event, f in order if event == "dispatch.suite_submitted"]
    # One id per submitted seed job, exactly as the fake handed them out.
    assert fields["job_ids"] == ["fake-1", "fake-2", "fake-3"]
    assert len(recording_backend.submitted) == 3
    # ...plus the build job, so the announced count is the drained count.
    assert fields["jobs"] == 3 + (1 if fields.get("build_job") else 0)


# ------------------------------------------------- #405: retry at collect


LICENSE_BANNER = "Queuing for License... (Licensed number of users already reached)\n"


class _RetryBackend(_FakeBackend):
    """A fleet whose jobs die the way a license-queue kill dies.

    Each submitted job writes the sim's own capture (with or without the
    queue banner) and then either leaves no envelope — reported by
    ``collect_telemetry`` as a scheduler ``TIMEOUT``, which is exactly the
    #405 shape — or, from ``passes_on_attempt`` onward, writes a PASS.
    """

    def __init__(self, *, banner=True, passes_on_attempt=None, state="TIMEOUT"):
        super().__init__(write_results=False)
        self.banner = banner
        self.passes_on_attempt = passes_on_attempt
        self.state = state
        self.attempts: dict[str, int] = {}
        self.delays: list[float] = []
        self.log_paths: list = []
        self.states: dict[str, str] = {}
        self.wait_calls = 0

    def submit(self, spec, *, dependency=None, delay_sec=0.0):
        self.submitted.append(spec)
        self.dependencies.append(dependency)
        self.delays.append(delay_sec)
        self.log_paths.append(spec.log_path)
        attempt = self.attempts.get(spec.test_name, 0) + 1
        self.attempts[spec.test_name] = attempt
        job_id = f"fake-{len(self.submitted)}"

        # Where the sim's own output lands — per run for a seed fan-out,
        # which is also the granularity classification reads it back at.
        artefacts = Path(spec.suite_dir) / "artefacts" / spec.test_name
        if spec.run_id is not None:
            artefacts = artefacts / f"run-{spec.run_id:04d}"
        artefacts.mkdir(parents=True, exist_ok=True)
        (artefacts / "test.log").write_text(
            LICENSE_BANNER if self.banner else "sim started\nrunning...\n"
        )

        if self.passes_on_attempt is not None and attempt >= self.passes_on_attempt:
            write_result_json(
                spec.result_json,
                test_name=spec.test_name,
                run_id=spec.run_id,
                results=TestPassResults(name=spec.test_name + "/results"),
                run_token=read_plan_token(spec.plan_path) if spec.plan_path else None,
            )
            self.states[job_id] = "COMPLETED"
        else:
            self.states[job_id] = self.state
        return JobHandle(job_id=job_id, spec=spec)

    def submit_array(self, specs, *, array_dir, max_parallel=None, dependency=None):
        return [self.submit(spec, dependency=dependency) for spec in specs]

    def wait_all(self, handles):
        self.wait_calls += 1
        super().wait_all(handles)

    def collect_telemetry(self, handles):
        return {h.job_id: {"state": self.states.get(h.job_id)} for h in handles}


def _use_backend(monkeypatch: pytest.MonkeyPatch, backend):
    monkeypatch.setattr(
        rtl_buddy_module,
        "create_dispatch_backend",
        lambda name, cfg: backend if name not in (None, "local") else None,
    )
    return backend


def _enable_retry(project: Path, *, attempts=2, backoff=5, cap=20, jitter=0.0):
    """Write a deterministic retry budget (jitter off keeps delays exact)."""
    root_cfg = project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text()
        + "\ncfg-dispatch:\n"
        + "  retry:\n"
        + f"    attempts: {attempts}\n"
        + f"    backoff-sec: {backoff}\n"
        + f"    backoff-max-sec: {cap}\n"
        + f"    jitter: {jitter}\n"
        + "    on: [license-queue]\n"
    )


def _rows(result):
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    return {r["name"]: r for r in json.loads(payload_line)["payload"]["results"]}


def test_license_queue_kill_is_retried_and_the_retry_can_pass(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _enable_retry(minimal_project)
    backend = _use_backend(monkeypatch, _RetryBackend(passes_on_attempt=2))

    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    assert _rows(result)["basic"]["result"] == "PASS"
    # Two submissions of the same test, and the retry waited on its own.
    assert [spec.test_name for spec in backend.submitted] == ["basic", "basic"]
    assert backend.wait_calls == 2
    # First submission unheld; the retry held for the first backoff step.
    assert backend.delays == [0.0, 5.0]


def test_retry_emits_a_console_event_naming_attempt_delay_and_classifier(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _enable_retry(minimal_project)
    _use_backend(monkeypatch, _RetryBackend(passes_on_attempt=2))

    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    # A green run that needed two attempts must not read like one that
    # needed none — and rb CLI events are only visible in the output.
    assert "retrying basic" in result.output
    assert "license-queue" in result.output
    assert "attempt 1 of 2" in result.output


def test_a_hung_test_is_not_retried(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Same TIMEOUT, no queue banner: the reservation was simply used up."""
    _enable_retry(minimal_project)
    backend = _use_backend(monkeypatch, _RetryBackend(banner=False))

    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 1, result.output
    assert _rows(result)["basic"]["result"] == "FAIL"
    assert [spec.test_name for spec in backend.submitted] == ["basic"]
    assert "retrying basic" not in result.output


def test_a_failed_job_is_not_retried_even_with_the_banner(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """FAILED is the job's own outcome, not the scheduler taking it away."""
    _enable_retry(minimal_project)
    backend = _use_backend(monkeypatch, _RetryBackend(state="FAILED"))

    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 1, result.output
    assert [spec.test_name for spec in backend.submitted] == ["basic"]


def test_exhausted_budget_fails_and_says_how_many_attempts(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A vanished job never scores green, however many attempts it got."""
    _enable_retry(minimal_project, attempts=2)
    backend = _use_backend(monkeypatch, _RetryBackend(passes_on_attempt=None))

    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 1, result.output
    row = _rows(result)["basic"]
    assert row["result"] == "FAIL"
    assert "after 3 attempts" in row["desc"]
    # One initial submission plus the two the budget allows; the delays
    # double, and the retries carry no build dependency (the build job has
    # long since left the queue).
    assert len(backend.submitted) == 3
    assert backend.delays == [0.0, 5.0, 10.0]
    assert backend.dependencies[1:] == [None, None]


def test_backoff_is_capped(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _enable_retry(minimal_project, attempts=3, backoff=5, cap=8)
    backend = _use_backend(monkeypatch, _RetryBackend(passes_on_attempt=None))

    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 1, result.output
    assert backend.delays == [0.0, 5.0, 8.0, 8.0]
    # Each attempt's log names its own attempt, not every attempt before it.
    assert [Path(p).name for p in backend.log_paths] == [
        "fake-single.log",
        "fake-single-retry1.log",
        "fake-single-retry2.log",
        "fake-single-retry3.log",
    ]


def test_each_attempt_keeps_its_own_scheduler_log(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The first attempt's banner is the evidence for the retry — keep it."""
    _enable_retry(minimal_project, attempts=1)
    backend = _use_backend(monkeypatch, _RetryBackend(passes_on_attempt=None))

    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 1, result.output
    first, retried = backend.log_paths
    assert Path(first).name == "fake-single.log"
    assert Path(retried).name == "fake-single-retry1.log"
    # The envelope path is the one contract the job and the head share, so
    # it must NOT move between attempts.
    assert backend.submitted[0].result_json == backend.submitted[1].result_json


def test_without_a_retry_block_a_license_queue_kill_still_fails_once(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Default off: identical behaviour to before #405."""
    backend = _use_backend(monkeypatch, _RetryBackend(passes_on_attempt=2))

    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 1, result.output
    assert _rows(result)["basic"]["result"] == "FAIL"
    assert len(backend.submitted) == 1
    assert backend.wait_calls == 1
    assert "after" not in _rows(result)["basic"]["desc"].split("(")[0]


def test_randtest_seeds_retry_independently(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """Retry is per (test, run_id), so one queued seed does not resubmit all."""
    _enable_retry(minimal_project, attempts=1)
    backend = _use_backend(monkeypatch, _RetryBackend(passes_on_attempt=None))

    result, _ = _invoke(["--machine", "randtest", "basic", "2", "--dispatch", "slurm"])
    assert result.exit_code == 1, result.output
    # Two seeds, each retried once.
    assert [spec.run_id for spec in backend.submitted] == [1, 2, 1, 2]
    assert backend.delays == [0.0, 0.0, 5.0, 5.0]
