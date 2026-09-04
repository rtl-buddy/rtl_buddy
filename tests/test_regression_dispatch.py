"""Dispatched regression flow tests (#351 P1).

Exercise ``rb regression --dispatch ...`` end-to-end over the
``minimal_project`` fixture with a fake backend and a stubbed
``TestRunner``: head-node build pass, fan-out, collection, failure
mapping, and result ordering — no scheduler or simulator involved.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

import rtl_buddy.rtl_buddy as rtl_buddy_module
from rtl_buddy.dispatch.base import DispatchBackend, JobHandle

# Aliased so pytest does not try to collect the dataclass as a test class.
from rtl_buddy.dispatch.base import TestJobSpec as SimJobSpec
from rtl_buddy.dispatch.plan import read_plan_configs, read_plan_token
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.result_io import write_build_result_json, write_result_json
from rtl_buddy.runner.test_results import (
    CompileFailResults,
    EarlyStopResults,
    SimTimeoutResults,
    TestPassResults,
)
from rtl_buddy.seed_mode import SeedMode


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
        self.extra_waits = []
        # Job ids each collect_telemetry pass asked about, so a test can
        # prove the build handle joined the first query and only that one
        # (#495).
        self.telemetry_queries = []

    def submit_build(self, spec):
        self.build_submitted.append(spec)
        return JobHandle(job_id="fake-build", spec=spec)

    def submit(self, spec, *, dependency=None, delay_sec=0.0):
        # `delay_sec` is accepted (and ignored) so the base fake matches the
        # DispatchBackend ABC: a backend that does not take the retry
        # backoff kwarg is exactly the out-of-tree breakage #405 introduced,
        # and nothing would catch it if only the retry fake had it.
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

    def wait_all(self, handles, *, extra_wait=0.0):
        self.waited = True
        self.extra_waits.append(extra_wait)

    def cancel_all(self, handles):
        self.cancelled = True


class _StubBuildRunner:
    """TestRunner stand-in for the head-node build pass."""

    canned = None
    inits = []
    # The compile record VlogSim stamps on itself (#495); the in-process
    # path folds it into every run's result envelope.
    compile_record = {"duration_sec": 2.5, "builder": "stub", "reused": False}

    def __init__(self, **kwargs):
        type(self).inits.append(kwargs)

    def run(self):
        return type(self).canned

    def run_multiple(self, run_ids):
        return [type(self).canned for _ in run_ids]

    @property
    def last_compile(self):
        return type(self).compile_record


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
        _backend_factory(backend),
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


def _write_colocated_suites(project: Path) -> Path:
    """Add a second tests config with distinct names in the same directory."""
    second = project / "other-tests.yaml"
    second.write_text(
        (project / "tests.yaml")
        .read_text()
        .replace("  - name: basic\n", "  - name: other_basic\n")
        .replace("  - name: extra\n", "  - name: other_extra\n")
    )
    (project / "regression.yaml").write_text(
        "rtl-buddy-filetype: reg_config\n"
        "test-configs:\n  - tests.yaml\n  - other-tests.yaml\n"
    )
    return second


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
        def submit(self, spec, *, dependency=None, delay_sec=0.0):
            seen_parent_exists.append(spec.log_path.parent.is_dir())
            return super().submit(spec)

    backend = _CheckBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
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
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
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
    _mark_stub_builder_verilator(minimal_project)

    class _CompileFailBuild(_FakeBackend):
        def __init__(self):
            super().__init__(write_results=False)  # sim envelope never appears

        def submit_build(self, spec):
            write_build_result_json(spec.result_json, built=[], failed=["basic"])
            return super().submit_build(spec)

    backend = _CompileFailBuild()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
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


def test_build_compile_failure_puts_the_real_error_in_the_summary(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The row names the build job, its exit status and its error (#498).

    The failure this replaces: the sim job retried the compile under its
    own reservation, was OOM-killed, wrote `%Error: Verilator threw signal
    9` over the build's compile.log, and the summary said `Compile failed`
    pointing at that file. Three rounds of raising compile memory went by
    before anyone opened `.dispatch/build-<id>.log` and found a one-line
    lint error.
    """
    _mark_stub_builder_verilator(minimal_project)

    class _CompileFailBuild(_FakeBackend):
        def submit_build(self, spec):
            write_build_result_json(
                spec.result_json,
                built=["extra"],
                failed=["basic"],
                builds=[
                    {
                        "test": "basic",
                        "builder": "verilator",
                        "returncode": 1,
                        "transcript": os.path.join("artefacts", "basic", "compile.log"),
                        "error_tail": [
                            "=== stderr ===",
                            "%Error: src/top.sv:3:7: Signal is not driven: 'q'",
                            "%Error: Exiting due to 1 error(s)",
                        ],
                    }
                ],
            )
            return super().submit_build(spec)

        def submit(self, spec, *, dependency=None, delay_sec=0.0):
            # The real gated job declines to recompile and reports the
            # build's verdict; its envelope is a CompileFail with the
            # generic desc when it predates #498, which is the case the
            # head still has to enrich.
            self.job_result = "FAIL" if spec.test_name == "basic" else "PASS"
            return super().submit(spec, dependency=dependency, delay_sec=delay_sec)

    backend = _CompileFailBuild()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 1, result.output

    # Every gated job was handed the envelope it needs to make that call.
    gated = {spec.test_name: spec for spec in backend.submitted}
    assert gated["basic"].expect_prebuilt is True
    assert gated["basic"].build_result_json == backend.build_submitted[0].result_json

    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    rows = {r["name"]: r for r in json.loads(payload_line)["payload"]["results"]}
    desc = rows["basic"]["desc"]
    assert rows["basic"]["result"] == "FAIL"
    assert desc.startswith("compile failed in build job fake-build (exit 1)")
    assert "Signal is not driven" in desc
    assert desc != "Compile failed"
    # One line: the summary renders it in a table cell.
    assert "\n" not in desc
    # A test the build actually built keeps its own verdict untouched.
    assert "compile failed in build job" not in rows["extra"]["desc"]
    # The rewrite is durable, not just rendered: `rb graph results` re-reads
    # the envelope, which would otherwise still say `Compile failed` (#498
    # review).
    envelope = json.loads(Path(gated["basic"].result_json).read_text())
    assert envelope["result"]["results"]["desc"] == desc


def test_a_sim_failure_is_not_relabelled_as_the_build_job_s_compile_error(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """`failed` says a compile failed, not that THIS row's failure was it.

    A config the build job failed to compile can still have a sim job that
    recompiled successfully (the stamp path is per-key) and then failed in
    simulation. Rewriting that row's desc would replace a real diagnosis
    with a guess (#498).
    """
    _mark_stub_builder_verilator(minimal_project)

    class _SimFailAfterBuildFail(_FakeBackend):
        def submit_build(self, spec):
            write_build_result_json(spec.result_json, built=[], failed=["basic"])
            return super().submit_build(spec)

        def submit(self, spec, *, dependency=None, delay_sec=0.0):
            handle = super().submit(spec, dependency=dependency, delay_sec=delay_sec)
            if spec.test_name == "basic":
                write_result_json(
                    spec.result_json,
                    test_name=spec.test_name,
                    run_id=spec.run_id,
                    results=SimTimeoutResults(name=spec.test_name + "/results"),
                    run_token=read_plan_token(spec.plan_path),
                )
            return handle

    backend = _SimFailAfterBuildFail()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 1, result.output

    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    rows = {r["name"]: r for r in json.loads(payload_line)["payload"]["results"]}
    assert rows["basic"]["desc"] == "Sim hit timeout"


def test_an_evidence_less_build_failure_keeps_the_retry_s_own_compile_fail(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The head's rewrite demands the same compiler evidence as the sim's gate.

    A `failed` entry whose record carries no returncode is a setup or worker
    error; the sim job saw no evidence, retried, and here failed its *own*
    compile. Rewriting that generic desc would attribute the retry's genuine
    compile failure to a build job whose compiler never ran (#498 review).
    """
    _mark_stub_builder_verilator(minimal_project)

    class _EvidencelessBuildFail(_FakeBackend):
        def submit_build(self, spec):
            write_build_result_json(
                spec.result_json,
                built=["extra"],
                failed=["basic"],
                builds=[
                    {
                        "test": "basic",
                        "builder": "verilator",
                        "error_tail": ["PRE hook raised: OSError: license server"],
                    }
                ],
            )
            return super().submit_build(spec)

        def submit(self, spec, *, dependency=None, delay_sec=0.0):
            self.job_result = "FAIL" if spec.test_name == "basic" else "PASS"
            return super().submit(spec, dependency=dependency, delay_sec=delay_sec)

    backend = _EvidencelessBuildFail()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
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
    assert rows["basic"]["desc"] == "Compile failed"


def test_an_inputs_changed_retry_s_own_failure_is_not_relabelled(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A sha-bearing record plus a generic desc means the sim retried.

    A current-generation sim job suppresses the retry on matching inputs
    and stamps the build prefix into its own desc — so a desc still saying
    `Compile failed` beside a `fingerprint_sha` record is the retry's own
    failure after input drift, not the build's stale verdict (#498 review).
    """
    _mark_stub_builder_verilator(minimal_project)

    class _DriftedBuildFail(_FakeBackend):
        def submit_build(self, spec):
            write_build_result_json(
                spec.result_json,
                built=["extra"],
                failed=["basic"],
                builds=[
                    {
                        "test": "basic",
                        "builder": "verilator",
                        "returncode": 1,
                        "fingerprint_sha": "0" * 64,
                        "error_tail": ["%Error: the OLD sources' error"],
                    }
                ],
            )
            return super().submit_build(spec)

        def submit(self, spec, *, dependency=None, delay_sec=0.0):
            self.job_result = "FAIL" if spec.test_name == "basic" else "PASS"
            return super().submit(spec, dependency=dependency, delay_sec=delay_sec)

    backend = _DriftedBuildFail()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
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
    assert rows["basic"]["desc"] == "Compile failed"
    assert "the OLD sources' error" not in rows["basic"]["desc"]


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
    # A suite whose command root is not shared keeps the established flat
    # .dispatch layout.
    dispatch_root = minimal_project / "artefacts" / ".dispatch"
    assert Path(build.plan_path).parent == dispatch_root
    assert Path(build.result_json).parent == dispatch_root
    assert Path(build.log_path).parent == dispatch_root


def test_dispatch_suite_identity_is_stable_and_filesystem_safe(tmp_path: Path):
    config = tmp_path / "suite config !.yaml"
    identity = rtl_buddy_module._dispatch_suite_identity(config)
    assert identity == rtl_buddy_module._dispatch_suite_identity(config)
    stem, separator, digest = identity.rpartition("-")
    assert separator and stem == "suite_config"
    assert len(digest) == 12 and set(digest) <= set("0123456789abcdef")
    assert identity != rtl_buddy_module._dispatch_suite_identity(
        tmp_path / "other" / config.name
    )


# ------------------------------------------------- P2: arrays / cross-suite


class _RecordingBackend(_FakeBackend):
    """FakeBackend that also records array submissions and wait calls."""

    def __init__(self, telemetry=None, build_result=None, **kwargs):
        super().__init__(**kwargs)
        self.array_calls = []
        self.wait_calls = 0
        self.telemetry = telemetry or {}
        # What the build job "wrote" — {built, failed, builds} (#495). The
        # base fake never runs a build job, so without this the head has no
        # build envelope to read compile records out of.
        self.build_result = build_result

    def submit_build(self, spec):
        handle = super().submit_build(spec)
        if self.build_result is not None:
            write_build_result_json(
                spec.result_json,
                built=self.build_result.get("built", []),
                failed=self.build_result.get("failed", []),
                builds=self.build_result.get("builds"),
            )
        return handle

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

    def wait_all(self, handles, *, extra_wait=0.0):
        self.wait_calls += 1
        super().wait_all(handles, extra_wait=extra_wait)

    def collect_telemetry(self, handles):
        self.telemetry_queries.append([h.job_id for h in handles])
        return self.telemetry


class _DelayedPlanBackend(_RecordingBackend):
    """Consume every plan only when the regression begins its global wait."""

    def __init__(self):
        super().__init__(write_results=False)
        self.consumed_build_plans = {}
        self.consumed_sim_plans = {}

    def submit_build(self, spec):
        self.build_submitted.append(spec)
        return JobHandle(job_id=f"delayed-build-{len(self.build_submitted)}", spec=spec)

    def submit(self, spec, *, dependency=None, delay_sec=0.0):
        self.submitted.append(spec)
        self.dependencies.append(dependency)
        return JobHandle(job_id=f"delayed-sim-{len(self.submitted)}", spec=spec)

    def wait_all(self, handles, *, extra_wait=0.0):
        # This is intentionally the first plan read. Both suites have already
        # submitted, matching a scheduler that leaves the first suite queued
        # until the second suite has replaced any colliding scratch files.
        for spec in self.build_submitted:
            self.consumed_build_plans[Path(spec.test_config_path).name] = [
                cfg.get_name() for cfg in read_plan_configs(spec.plan_path)
            ]
        for spec in self.submitted:
            configs = {cfg.get_name() for cfg in read_plan_configs(spec.plan_path)}
            self.consumed_sim_plans[Path(spec.test_config_path).name] = configs
            assert spec.test_name in configs
            write_result_json(
                spec.result_json,
                test_name=spec.test_name,
                run_id=spec.run_id,
                results=TestPassResults(name=spec.test_name + "/results"),
                run_token=read_plan_token(spec.plan_path),
            )
        super().wait_all(handles, extra_wait=extra_wait)


@pytest.fixture
def recording_backend(monkeypatch: pytest.MonkeyPatch) -> _RecordingBackend:
    backend = _RecordingBackend()
    monkeypatch.setattr(
        rtl_buddy_module,
        "create_dispatch_backend",
        _backend_factory(backend),
    )
    return backend


def test_regression_namespaces_colocated_suites_and_waits_once(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    recording_backend: _RecordingBackend,
):
    # Two distinct configs share one command root. Their jobs must all be in
    # flight before the single wait, without sharing any suite-scoped path.
    _mark_stub_builder_verilator(minimal_project)
    _write_colocated_suites(minimal_project)
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    assert len(recording_backend.submitted) == 2
    assert len(recording_backend.build_submitted) == 2
    assert recording_backend.wait_calls == 1

    plans = [Path(spec.plan_path) for spec in recording_backend.build_submitted]
    assert len(set(plans)) == 2
    namespaces = {plan.parent for plan in plans}
    dispatch_root = minimal_project / "artefacts" / ".dispatch"
    assert {path.parent for path in namespaces} == {dispatch_root}
    for namespace in namespaces:
        stem, separator, digest = namespace.name.rpartition("-")
        assert separator and stem
        assert len(digest) == 12 and set(digest) <= set("0123456789abcdef")

    for build in recording_backend.build_submitted:
        namespace = Path(build.plan_path).parent
        assert Path(build.result_json).parent == namespace
        assert Path(build.log_path).parent == namespace
    assert {
        Path(call["array_dir"]).parent for call in recording_backend.array_calls
    } == namespaces

    planned = {
        Path(json.loads(path.read_text())["suite_config"]).name: [
            test["name"] for test in json.loads(path.read_text())["tests"]
        ]
        for path in plans
    }
    assert planned == {"tests.yaml": ["basic"], "other-tests.yaml": ["other_basic"]}


def test_colocated_suite_plans_survive_until_delayed_job_consumption(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Queued jobs consume their own plan after every suite has submitted."""
    _mark_stub_builder_verilator(minimal_project)
    _write_colocated_suites(minimal_project)
    backend = _DelayedPlanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )

    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    expected = {"tests.yaml": ["basic"], "other-tests.yaml": ["other_basic"]}
    assert backend.consumed_build_plans == expected
    assert {
        name: sorted(tests) for name, tests in backend.consumed_sim_plans.items()
    } == expected


def test_a_later_suites_sweep_hook_does_not_alter_an_earlier_suites_submission(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    recording_backend: _RecordingBackend,
    monkeypatch: pytest.MonkeyPatch,
):
    """Every suite is planned before any submits; each submits from its own env.

    The second suite's sweep hook exports `SBATCH_NTASKS=64` in this process.
    Pre-expansion runs that hook before the first suite's jobs go out, so
    without a per-suite snapshot the first suite's build and array would
    inherit the second suite's environment (and its analysis would record
    the wrong override). After the last submission the process keeps the
    hook's environment, since a retry is documented as a fresh sbatch from
    whatever the head holds by then.
    """
    monkeypatch.setenv("SBATCH_NTASKS", "4")
    _mark_stub_builder_verilator(minimal_project)
    second = _write_colocated_suites(minimal_project)
    second.write_text(
        second.read_text().replace(
            "    sweep:\n", "    sweep:\n      path: export-sweep.py\n"
        )
    )
    (minimal_project / "export-sweep.py").write_text(
        "import os\nos.environ['SBATCH_NTASKS'] = '64'\nout_test_cfgs = [test_cfg]\n"
    )

    seen = {"build": {}, "sim": {}}
    real_submit_build = recording_backend.submit_build
    real_submit = recording_backend.submit

    def submit_build_recording_env(spec):
        seen["build"][Path(spec.test_config_path).name] = os.environ["SBATCH_NTASKS"]
        return real_submit_build(spec)

    def submit_recording_env(spec, **kwargs):
        seen["sim"][spec.test_name] = os.environ["SBATCH_NTASKS"]
        return real_submit(spec, **kwargs)

    monkeypatch.setattr(recording_backend, "submit_build", submit_build_recording_env)
    monkeypatch.setattr(recording_backend, "submit", submit_recording_env)

    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    assert seen["build"] == {"tests.yaml": "4", "other-tests.yaml": "64"}
    assert seen["sim"] == {"basic": "4", "other_basic": "64"}
    assert os.environ["SBATCH_NTASKS"] == "64"


def test_colocated_duplicate_test_artifact_is_rejected_before_submission(
    minimal_project: Path,
    recording_backend: _RecordingBackend,
):
    duplicate = minimal_project / "duplicate-tests.yaml"
    duplicate.write_text((minimal_project / "tests.yaml").read_text())
    (minimal_project / "regression.yaml").write_text(
        "rtl-buddy-filetype: reg_config\n"
        "test-configs:\n  - tests.yaml\n  - duplicate-tests.yaml\n"
    )

    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code != 0, result.output
    assert recording_backend.build_submitted == []
    assert recording_backend.submitted == []
    assert recording_backend.array_calls == []
    assert "expanded tests 'basic'" in result.output
    assert "tests.yaml" in result.output
    assert "duplicate-tests.yaml" in result.output
    assert str(minimal_project / "artefacts" / "basic") in result.output


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
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    envelope = json.loads(backend.submitted[0].result_json.read_text())
    assert envelope["telemetry"]["state"] == "COMPLETED"
    assert envelope["telemetry"]["elapsed_s"] == 5


# ------------------------------------------- #495: build-job telemetry


def _build_telemetry_backend(monkeypatch, *, builds=None, build_telemetry=None):
    """A fleet whose build job left both a result envelope and a sacct row."""
    telemetry = {
        "fake-1": {"state": "COMPLETED", "elapsed_s": 5, "timelimit_s": 3600},
    }
    if build_telemetry is not None:
        telemetry["fake-build"] = build_telemetry
    backend = _RecordingBackend(
        telemetry=telemetry,
        build_result={
            "built": ["basic"],
            "failed": [],
            "builds": builds
            if builds is not None
            else [
                {
                    "test": "basic",
                    "builder": "hook-chosen-builder",
                    "duration_sec": 42.5,
                    "reused": False,
                    "group": "obj_dir_cafe",
                }
            ],
        },
    )
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    return backend


def test_cpu_overrides_are_snapshotted_at_submit_not_reread_at_analysis(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The environment can move between a suite's submit and its analysis.

    A dispatched regression submits every suite before collecting any, and
    a suite's sweep hook is `exec()`d in this same process (hooks.py), so a
    later suite's hook can set or unset `SBATCH_*` in the window between
    this suite's jobs going out and its advice being computed. Re-reading
    `os.environ` at analysis would judge these jobs by a different
    environment: the wrong cpu denominator, and an `edit_hint` naming an
    override that was never active for them (#505 review).

    `wait_all` is that window — it runs after the last submit and before
    the first collect — so mutating the environment there reproduces the
    hook's effect exactly, without needing a second suite.
    """
    monkeypatch.setenv("SBATCH_NTASKS", "4")
    backend = _build_telemetry_backend(
        monkeypatch,
        build_telemetry={
            "state": "COMPLETED",
            "elapsed_s": 100,
            "timelimit_s": 7200,
            "alloc_cpus": 8,
            "req_cpus": 8,  # 4 tasks x the generated 2 cpus
            "total_cpu_s": 200,  # 0.25 efficiency
        },
    )
    backend.telemetry["fake-1"] = {
        "state": "COMPLETED",
        "elapsed_s": 100,
        "timelimit_s": 3600,
        "alloc_cpus": 8,
        "req_cpus": 8,
        "total_cpu_s": 200.0,  # 0.25 efficiency
    }

    # Stand in for the later suite's sweep hook: same process, same window.
    real_wait_all = backend.wait_all

    def wait_all_then_change_the_environment(handles, **kwargs):
        os.environ["SBATCH_NTASKS"] = "64"
        return real_wait_all(handles, **kwargs)

    monkeypatch.setattr(backend, "wait_all", wait_all_then_change_the_environment)

    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    assert os.environ["SBATCH_NTASKS"] == "64", "the stand-in hook must have run"

    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    cpus_rows = [a for a in advice if a["resource"] == "cpus"]
    assert cpus_rows, "both the test and the build job are over-reserved"
    # Both halves of the suite's advice describe ONE submission: the
    # environment as it stood when these jobs were sent.
    assert {a["test"] for a in cpus_rows} == {"basic", "(build job)"}
    for row in cpus_rows:
        note = row["edit_hint"]["note"]
        assert "`SBATCH_NTASKS=4`" in note, note
        assert "64" not in note, note


def test_collect_attaches_the_build_jobs_own_telemetry_to_its_envelope(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The build job's sacct row travels with its artifact too (#495).

    Until now the one job in a dispatched fleet whose reservation nobody
    could check afterwards was the one holding the whole fan-out.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _build_telemetry_backend(
        monkeypatch,
        build_telemetry={"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
    )
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    build_envelope = json.loads(
        Path(backend.build_submitted[0].result_json).read_text()
    )
    assert build_envelope["telemetry"]["elapsed_s"] == 60
    # Additive: the half the head has always read is untouched.
    assert build_envelope["built"] == ["basic"]
    # The build handle rode along in the first (and only) telemetry query.
    assert backend.telemetry_queries == [["fake-build", "fake-1"]]


def test_sim_rows_and_envelopes_carry_the_build_jobs_compile_record(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The compile a test never ran itself still shows up on its row (#495).

    The record is folded into the envelope's nested ``result.results``,
    which is where `rb graph results` reads a run's payload from — a
    top-level key would travel with the artifact and stay invisible.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _build_telemetry_backend(monkeypatch)
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    envelope = json.loads(Path(backend.submitted[0].result_json).read_text())
    compile_block = envelope["result"]["results"]["compile"]
    assert compile_block["duration_sec"] == 42.5
    # The build job's observation wins over the builder the head resolved
    # before submitting: a preproc hook can move it, and the envelope is
    # what actually ran.
    assert compile_block["builder"] == "hook-chosen-builder"
    assert compile_block["reused"] is False
    # `group` is build-job bookkeeping, not part of the per-run record.
    assert "group" not in compile_block


def test_an_old_build_envelope_without_compile_records_still_collects(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """Mixed-version fleets degrade, never fail (#495).

    A build job from before the records writes no ``builds`` key; the head
    must simply leave the sim rows without a compile block.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _build_telemetry_backend(monkeypatch, builds=None)
    backend.build_result["builds"] = None  # exactly the old envelope shape
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    build_envelope = json.loads(
        Path(backend.build_submitted[0].result_json).read_text()
    )
    assert "builds" not in build_envelope
    envelope = json.loads(Path(backend.submitted[0].result_json).read_text())
    assert "compile" not in envelope["result"]["results"]


def test_a_retry_pass_does_not_re_query_or_re_attach_the_build_job(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Build telemetry is a first-pass fact (#495).

    A retry pass collects a resubmitted *sim* subset; the build job is
    never resubmitted and was already finished by the fleet-wide wait, so
    re-querying it would buy a second identical row and a second identical
    write.
    """
    # Share-build capable, so the suite actually gets a build job to leave
    # out of the second query.
    _mark_stub_builder_verilator(minimal_project)
    _enable_retry(minimal_project)
    backend = _use_backend(monkeypatch, _RetryBackend(passes_on_attempt=2))

    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    assert len(backend.telemetry_queries) == 2
    assert backend.telemetry_queries[0][0] == "fake-build"
    assert "fake-build" not in backend.telemetry_queries[1]


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
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
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
    other_dir = minimal_project / "other"
    other_dir.mkdir()
    other_suite = other_dir / "tests.yaml"
    other_suite.write_text(
        (minimal_project / "tests.yaml")
        .read_text()
        .replace("model_path: models.yaml", "model_path: ../models.yaml")
    )
    (minimal_project / "regression.yaml").write_text(
        "rtl-buddy-filetype: reg_config\n"
        "test-configs:\n  - tests.yaml\n  - other/tests.yaml\n"
    )
    backend = _RecordingBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
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
    # Each suite is entered for planning, submission, and collection.
    assert entered.count(str(minimal_project / "tests.yaml")) == 3
    assert entered.count(str(other_suite)) == 3


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
    monkeypatch: pytest.MonkeyPatch,
):
    """The one allocation is sized max(sim, compile) field by field (#358).

    ``parallel: 4`` is set to prove it does NOT reach here (#495): a sim
    job that compiles for itself runs exactly one build, whatever the
    build job would have been allowed to do concurrently. That holds for
    the ``compile_floor`` rows as well as the reservation — the floor is
    what clamps a `reduce` suggestion, so a scaled one would advise every
    in-job compile up to N times the cpus it can use.
    """
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "00:20:00"\n'
        '  compile:\n    cpus: 8\n    mem: 16G\n    time: "00:10:00"\n'
        "    parallel: 4\n",
    )
    rows_analyzed: list[dict] = []
    original = rtl_buddy_module.analyze_suite_reservations

    def _spy(suite_results, **kwargs):
        rows_analyzed.extend(suite_results)
        return original(suite_results, **kwargs)

    monkeypatch.setattr(rtl_buddy_module, "analyze_suite_reservations", _spy)

    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    resources = fake_backend.submitted[0].resources
    assert resources.cpus == 8  # compile needs more; NOT 4 x 8
    assert resources.mem == "16G"  # compile needs more
    assert resources.time == "00:20:00"  # sim needs more; compile's is smaller

    floors = [row["compile_floor"] for row in rows_analyzed if "compile_floor" in row]
    assert floors, "no in-job-compile row reached right-sizing"
    for floor in floors:
        assert floor == {"cpus": 8, "mem": "16G", "time": "00:10:00"}


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
    # Nothing asked for concurrency, so the reservation is unscaled and the
    # spec says so.
    assert fake_backend.build_submitted[0].parallel == 1


def _add_suite_compile(project: Path, block: str):
    """Prepend a suite-level ``compile:`` block to the fixture's tests.yaml.

    Top of file, after the filetype line, which is where the docs put it.
    """
    tests_yaml = project / "tests.yaml"
    body = tests_yaml.read_text()
    marker = "rtl-buddy-filetype: test_config\n"
    assert body.startswith(marker)
    tests_yaml.write_text(marker + block + body[len(marker) :])


def test_suite_compile_block_overrides_the_build_job_reservation(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A suite's own ``compile:`` beats cfg-dispatch, field by field (#497).

    The reported waste: one global compile reservation fences off the
    largest verilation in the repo for every leaf-cell bench's build job.
    The suite that genuinely needs the memory states it, and the cpus/time
    it says nothing about still come from cfg-dispatch.
    """
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "00:20:00"\n'
        '  compile:\n    cpus: 4\n    mem: 16G\n    time: "02:00:00"\n',
    )
    _add_suite_compile(minimal_project, "compile:\n  mem: 48G\n")
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0].resources
    assert build.mem == "48G"  # the suite's
    assert (build.cpus, build.time) == (4, "02:00:00")  # inherited
    # Sim jobs only simulate: the compile block, at either level, is not
    # allowed anywhere near their reservation.
    sim = fake_backend.submitted[0].resources
    assert (sim.cpus, sim.mem, sim.time) == (1, "2G", "00:20:00")


def test_suite_compile_block_scales_with_compile_parallel(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """`parallel` stays a cfg-dispatch knob and scales the suite's cpus too."""
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(2))
    _add_suite_compile(minimal_project, "compile:\n  cpus: 6\n  parallel: 8\n")
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    # `parallel: 8` in the suite block is an unknown key and is dropped;
    # the cluster-wide 2 still binds, capped at the 2 planned configs.
    assert build.parallel == 2
    assert build.resources.cpus == 12  # 2 x the suite's 6, not 2 x 4
    assert build.resources.mem == "16G"  # cfg-dispatch's, unscaled


def test_suite_compile_block_reaches_an_in_job_compile_reservation(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A builder that compiles in its own sim job gets the suite block too.

    The fixture's inferred "echo" family cannot share a build, so there is
    no build job at all — the compile reservation only ever shows up in the
    field-wise maximum that sizes the sim job. A suite block that stopped
    at the build job would leave exactly that case under-reserved (#497).
    """
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "00:20:00"\n'
        '  compile:\n    cpus: 2\n    mem: 4G\n    time: "00:10:00"\n',
    )
    _add_suite_compile(minimal_project, "compile:\n  cpus: 8\n  mem: 48G\n")
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    assert fake_backend.build_submitted == []
    resources = fake_backend.submitted[0].resources
    assert resources.cpus == 8  # the suite's compile, over cfg-dispatch's 2
    assert resources.mem == "48G"  # the suite's compile, over cfg-dispatch's 4G
    assert resources.time == "00:20:00"  # sim's is longer than compile's


def _add_third_test(project: Path):
    """A third planned config, so ``parallel`` can be the binding limit.

    The fixture ships two tests (basic at reglvl 0, extra at 5), which is
    not enough to tell a cap by the planned configs from a cap by the knob.
    """
    tests_yaml = project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text()
        + "\n".join(
            [
                "  - name: third",
                "    desc: third test entry",
                "    model: example",
                "    model_path: models.yaml",
                "    reglvl: 5",
                "    testbench: tb_basic",
            ]
        )
        + "\n"
    )


def _compile_parallel_config(parallel: int) -> str:
    return (
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "00:20:00"\n'
        '  compile:\n    cpus: 4\n    mem: 16G\n    time: "02:00:00"\n'
        f"    parallel: {parallel}\n"
    )


def test_build_job_cpus_scale_with_compile_parallel(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """N concurrent builds get N times the cpus — and only cpus (#495).

    The reported defect: one 16-CPU reservation ran eight ~1.1-core
    Verilations one after another while 24 sims waited. The reservation is
    what pays for the concurrency, so the head is the only place that can
    size it.
    """
    _mark_stub_builder_verilator(minimal_project)
    _add_third_test(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(2))
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    # 3 planned configs, knob of 2: the knob binds, not the cap.
    assert [spec.test_name for spec in fake_backend.submitted] == [
        "basic",
        "extra",
        "third",
    ]
    assert build.parallel == 2
    assert build.resources.cpus == 8  # 2 x 4
    # mem/time are the project's to size for N concurrent Verilations.
    assert (build.resources.mem, build.resources.time) == ("16G", "02:00:00")

    # The sim jobs are untouched: they only simulate.
    sim = fake_backend.submitted[0].resources
    assert (sim.cpus, sim.mem, sim.time) == (1, "2G", "00:20:00")


def test_build_job_parallel_is_capped_by_the_planned_configs(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """Two planned configs cannot keep three build slots busy (#495).

    Without the cap the head would reserve cpus for slots that are
    guaranteed to idle — the reservation grows and the wall clock does not.
    """
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(3))
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert build.parallel == 2  # basic + extra, not the configured 3
    assert build.resources.cpus == 8  # 2 x 4, not 3 x 4


def test_single_planned_config_leaves_the_build_reservation_alone(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """One config is one build: today's spec, byte for byte."""
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(4))
    # -l 0 plans "basic" alone (extra is reglvl 5).
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert build.parallel == 1
    assert build.resources.cpus == 4


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
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
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


def test_whole_core_rounding_produces_no_cpus_advice_end_to_end(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The head's own `--cpus-per-task` reaches right-sizing (#505).

    The project reserves the default 1 cpu; the site allocates a whole core
    and reports 2, with no `ReqCPUS` at all. Judged against the allocation a
    fully-busy single-threaded run measures 0.5 efficiency and every test
    gets advised down to the `cpus: 1` it already has. The row records what
    the head submitted, so the ratio is 1.0 and there is nothing to say.
    """
    backend = _RecordingBackend(
        telemetry={
            "fake-1": {
                "state": "COMPLETED",
                "elapsed_s": 100,
                "timelimit_s": 3600,
                "req_mem_bytes": 8 * 2**30,
                "alloc_cpus": 2,
                "total_cpu_s": 25.0,  # 0.125 eff vs the allocation, 0.25 vs 1
            }
        }
    )
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    assert [a for a in advice if a["resource"] == "cpus"] == []
    # `allocated` is on every finding, so the key set stays stable; it is
    # only ever non-null on a cpus row.
    assert advice, "the time/mem rows should still be there"
    assert all(a["allocated"] is None for a in advice)


@pytest.mark.parametrize(
    "sbatch_args,named",
    [
        ("[--cpus-per-task=4]", "--cpus-per-task=4"),
        # sbatch obeys the LAST occurrence of one option, and so must the hint.
        ("[--cpus-per-task=2, --cpus-per-task=4]", "--cpus-per-task=4"),
    ],
)
def test_an_sbatch_args_cpus_override_sends_the_analysis_back_to_reqcpus(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
    sbatch_args: str,
    named: str,
):
    """`sbatch-args` is appended last and wins, so the YAML is not the request.

    The project resolves the default 1 cpu, but `cfg-dispatch.sbatch-args`
    states a cpu request of 4 directly, and that is what the jobs run with.
    Recording the resolved 1 as the request would analyse a genuinely
    over-reserved run against cpus it never had and, with `cpus: 1` failing
    the `cpus > 1` guard, silently drop the finding. The head records
    nothing instead, so `ReqCPUS` carries it (#505 review). A task count
    rather than a cpu count is covered separately: the denominator is the
    same, the note is not.
    """
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text() + f"\ncfg-dispatch:\n  sbatch-args: {sbatch_args}\n"
    )
    backend = _RecordingBackend(
        telemetry={
            "fake-1": {
                "state": "COMPLETED",
                "elapsed_s": 100,
                "timelimit_s": 3600,
                "req_mem_bytes": 8 * 2**30,
                "alloc_cpus": 4,
                "req_cpus": 4,  # what the override actually asked for
                "total_cpu_s": 100.0,  # 0.25 efficiency against those 4
            }
        }
    )
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    _mark_stub_builder_verilator(minimal_project)
    # `-D` so the DEBUG line explaining the fallback reaches the console:
    # rb reconfigures the root logger, so caplog never sees a CLI run's
    # events and the output is what can be asserted on.
    result, _ = _invoke(
        [
            "-D",
            "--machine",
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "slurm",
        ]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    (cpus,) = [a for a in advice if a["resource"] == "cpus"]
    # Analysed against the 4 the override submitted, not the 1 the YAML
    # resolved to -- which would have been dropped by the `cpus > 1` guard.
    assert cpus["reserved"] == "4"
    assert cpus["allocated"] is None
    assert cpus["suggested"] == "2"  # ceil(4 x 0.25 x 1.5)
    # ...and the hint names the argument, not the field it masks: editing
    # `resources.cpus` would leave the next job's reservation where it is.
    assert cpus["edit_hint"]["path"] == "cfg-dispatch.sbatch-args"
    assert (
        f"sbatch-args `{named}` sets this job's cpu request, "
        "superseding tests[name=basic].resources.cpus" in cpus["edit_hint"]["note"]
    )
    # time still names its own field; a cpu argument supersedes nothing there.
    (time_row,) = [a for a in advice if a["resource"] == "time"]
    assert time_row["edit_hint"]["path"] == "tests[name=basic].resources.time"
    # ...and the run says why the advice came from sacct rather than from
    # the reservation, naming the argument responsible.
    assert "sbatch-args" in result.output
    assert named in result.output
    assert "ReqCPUS" in result.output


def test_an_sbatch_env_var_reaches_the_analysis_end_to_end(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """`SBATCH_NTASKS` is inherited by `subprocess.run`, so sbatch reads it.

    The project resolves the default 1 cpu and nothing in `sbatch-args`
    touches cpus, but the environment asks for four tasks — a four-cpu job
    that `requested_cpus` would call one, overstating efficiency fourfold.
    The environment is deliberately NOT sanitized; it is recognised
    instead, so the analysis falls back to `ReqCPUS` and the hint names the
    variable rather than a YAML field it masks (#505 review).
    """
    monkeypatch.setenv("SBATCH_NTASKS", "4")
    backend = _RecordingBackend(
        telemetry={
            "fake-1": {
                "state": "COMPLETED",
                "elapsed_s": 100,
                "timelimit_s": 3600,
                "req_mem_bytes": 8 * 2**30,
                "alloc_cpus": 4,
                "req_cpus": 4,  # 4 tasks x the generated 1 cpu
                "total_cpu_s": 100.0,  # 0.25 efficiency against those 4
            }
        }
    )
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    (cpus,) = [a for a in advice if a["resource"] == "cpus"]
    # Analysed against the 4 the environment asked for, not the 1 the YAML
    # resolved to — which the `cpus > 1` guard would have dropped.
    assert cpus["reserved"] == "4"
    assert cpus["suggested"] == "2"  # ceil(4 x 0.25 x 1.5)
    assert cpus["edit_hint"]["path"] == "env"
    assert "file" not in cpus["edit_hint"]
    assert (
        "`SBATCH_NTASKS=4` multiplies this job's cpu request"
        in (cpus["edit_hint"]["note"])
    )


def test_sbatch_cpus_per_task_in_the_environment_is_not_an_override(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The generated `--cpus-per-task` beats it, so nothing changes.

    Command line > environment is sbatch's own precedence, and every
    submit path states `--cpus-per-task`. Treating the variable as an
    override would discard a request the head knows and resurrect the
    spurious "reduce cpus to 1" this issue is about (#505 review).
    """
    monkeypatch.setenv("SBATCH_CPUS_PER_TASK", "4")
    backend = _RecordingBackend(
        telemetry={
            "fake-1": {
                "state": "COMPLETED",
                "elapsed_s": 100,
                "timelimit_s": 3600,
                "req_mem_bytes": 8 * 2**30,
                "alloc_cpus": 2,
                "req_cpus": 2,  # the site rounded the one cpu asked for
                "total_cpu_s": 50.0,  # 0.25 eff vs 2, 0.5 vs the requested 1
            }
        }
    )
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    assert [a for a in advice if a["resource"] == "cpus"] == []


@pytest.mark.parametrize(
    "arg",
    [
        # Node SELECTION: restricts which nodes and hardware threads may be
        # used; the generated `--cpus-per-task` still states the request.
        "--threads-per-core=2",
        "-B 2:4:1",
        # Placement MAXIMA: cap where the tasks `--ntasks` asked for may
        # land. Alone they request nothing at all (#505 review).
        "--ntasks-per-core=2",
        "--ntasks-per-socket=2",
        "--ntasks-per-gpu=2",
    ],
)
def test_a_placement_or_selection_arg_is_not_treated_as_a_cpu_override(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
    arg: str,
):
    """These shape placement; they do not set the cpu request.

    The generated `--cpus-per-task=1` still states what the job asks for,
    so the head knows the request and must keep using it. Reading one of
    these as an override would throw that away, fall back to a `ReqCPUS`
    the site rounded to 2, and resurrect the exact spurious "reduce cpus
    to 1" this issue is about — with the hint pointing at `sbatch-args`
    for good measure (#505 review).
    """
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text() + f"\ncfg-dispatch:\n  sbatch-args: [{arg}]\n"
    )
    backend = _RecordingBackend(
        telemetry={
            "fake-1": {
                "state": "COMPLETED",
                "elapsed_s": 100,
                "timelimit_s": 3600,
                "req_mem_bytes": 8 * 2**30,
                "alloc_cpus": 2,
                "req_cpus": 2,  # the site rounded the one cpu asked for
                "total_cpu_s": 50.0,  # 0.25 eff vs 2, 0.5 vs the requested 1
            }
        }
    )
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    assert [a for a in advice if a["resource"] == "cpus"] == []
    # ...and nothing was retargeted: the other rows still name the YAML.
    (time_row,) = [a for a in advice if a["resource"] == "time"]
    assert time_row["edit_hint"]["path"] == "tests[name=basic].resources.time"


def test_a_lone_ntasks_override_does_not_claim_to_take_the_suggestion(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """`--ntasks` is a task count, so "write 2 in here" would mean 2 tasks.

    End to end: one override argument, but not a cpu count. The finding
    still has to reach the reader with the whole-job figure, and the note
    must not tell them to put it into an argument that would not take it
    (#505 review).
    """
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text() + "\ncfg-dispatch:\n  sbatch-args: [--ntasks=4]\n"
    )
    backend = _RecordingBackend(
        telemetry={
            "fake-1": {
                "state": "COMPLETED",
                "elapsed_s": 100,
                "timelimit_s": 3600,
                "req_mem_bytes": 8 * 2**30,
                "alloc_cpus": 4,
                "req_cpus": 4,  # 4 tasks x the generated 1 cpu
                "total_cpu_s": 100.0,  # 0.25 efficiency against those 4
            }
        }
    )
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    (cpus,) = [a for a in advice if a["resource"] == "cpus"]
    assert cpus["reserved"] == "4"
    assert cpus["suggested"] == "2"  # ceil(4 x 0.25 x 1.5), whole-job
    assert cpus["edit_hint"]["path"] == "cfg-dispatch.sbatch-args"
    note = cpus["edit_hint"]["note"]
    assert "`--ntasks=4` multiplies this job's cpu request" in note
    assert "change it there" not in note


def test_orthogonal_sbatch_args_cpu_options_withhold_the_per_argument_edit(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """`--ntasks` x `--cpus-per-task` is a product, so neither takes the number.

    End to end: the request is 4 tasks of 2 cpus, the run uses a quarter of
    it, and the advice still has to reach the reader — but pointing at
    either argument alone would be wrong in both directions and the finding
    would recur (#505 review).
    """
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text()
        + "\ncfg-dispatch:\n  sbatch-args: [--ntasks=4, --cpus-per-task=2]\n"
    )
    backend = _RecordingBackend(
        telemetry={
            "fake-1": {
                "state": "COMPLETED",
                "elapsed_s": 100,
                "timelimit_s": 3600,
                "req_mem_bytes": 8 * 2**30,
                "alloc_cpus": 8,
                "req_cpus": 8,  # 4 x 2
                "total_cpu_s": 200.0,  # 0.25 efficiency against those 8
            }
        }
    )
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(
        [
            "-D",
            "--machine",
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "slurm",
        ]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    (cpus,) = [a for a in advice if a["resource"] == "cpus"]
    assert cpus["reserved"] == "8"  # the product, from ReqCPUS
    assert cpus["suggested"] == "3"  # ceil(8 x 0.25 x 1.5), whole-job
    assert cpus["edit_hint"]["path"] == "cfg-dispatch.sbatch-args"
    note = cpus["edit_hint"]["note"]
    assert (
        "`--ntasks=4` and `--cpus-per-task=2` set this job's cpu request together"
        in note
    )
    assert "product" not in note
    assert "decompose it across them per sbatch's own precedence" in note
    # The DEBUG line lists both arguments, for the same reason.
    assert "`--ntasks=4` and `--cpus-per-task=2`" in result.output


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


def test_machine_payload_carries_build_job_reservation_advice(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The build job's own reservation gets a `compile` advice row (#495).

    It owns no suite_results row, so it is the one job in the fleet that
    per-test analysis can never see — and with `compile.parallel` it is
    also the one whose reservation a project is most likely to overshoot.
    Two builds over two slots is also the shape whose *cpus* row is
    withheld: see below.
    """
    _mark_stub_builder_verilator(minimal_project)
    _build_telemetry_backend(
        monkeypatch,
        builds=[
            {
                "test": name,
                "builder": "hook-chosen-builder",
                "duration_sec": 42.5,
                "reused": False,
                "group": f"obj_dir_{group}",
            }
            for name, group in (("basic", "cafe"), ("extra", "f00d"))
        ],
        build_telemetry={
            "state": "COMPLETED",
            "elapsed_s": 60,
            "timelimit_s": 7200,
            # 8 cpus (4 per build x parallel 2) that used 60 core-seconds of
            # the 480 they had: badly over-reserved.
            "alloc_cpus": 8,
            "total_cpu_s": 60,
        },
    )
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "01:00:00"\n'
        '  compile:\n    cpus: 4\n    mem: 8G\n    time: "02:00:00"\n'
        "    parallel: 2\n",
    )
    # -l 5 puts both fixture tests in the plan, so the head's cap does not
    # collapse `parallel: 2` back to 1 for a single-config suite.
    result, _ = _invoke(
        [
            "--machine",
            "regression",
            "-c",
            "regression.yaml",
            "-l",
            "5",
            "--dispatch",
            "slurm",
        ]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    build_advice = {a["resource"]: a for a in advice if a["phase"] == "compile"}

    # No cpus row: the job ran two build slots, so its efficiency counts
    # idle slots in the tail as well as under-used compilers and nothing in
    # sacct separates them (#496 review). The withholding itself, and the
    # reason it carries, is pinned in tests/test_dispatch_rightsize.py.
    assert "cpus" not in build_advice

    # time IS still advised: it is wall clock, which N concurrent builds do
    # not inflate, so it needs no note and no division.
    time_a = build_advice["time"]
    assert time_a["test"] == "(build job)"
    assert time_a["reserved"] == "02:00:00"
    assert time_a["direction"] == "reduce"
    assert time_a["edit_hint"]["path"] == "cfg-dispatch.compile.time"
    assert time_a["edit_hint"]["file"].endswith("root_config.yaml")
    assert "note" not in time_a["edit_hint"]


def test_build_advice_names_the_suite_file_for_a_field_the_suite_overrode(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The advice must point at the file that holds the winning value (#497).

    A suite-level `compile.time` beats cfg-dispatch, so advice that named
    `cfg-dispatch.compile.time` would send a project to edit a key that
    moves nothing. The suite block travels to the post-run analysis through
    the dispatch state, which is what this exercises end to end.
    """
    _mark_stub_builder_verilator(minimal_project)
    _build_telemetry_backend(
        monkeypatch,
        builds=[
            {
                "test": "basic",
                "builder": "hook-chosen-builder",
                "duration_sec": 42.5,
                "reused": False,
                "group": "obj_dir_cafe",
            }
        ],
        build_telemetry={
            "state": "COMPLETED",
            "elapsed_s": 60,
            "timelimit_s": 10800,
            "alloc_cpus": 4,
            "total_cpu_s": 60,
        },
    )
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "01:00:00"\n'
        '  compile:\n    cpus: 4\n    mem: 8G\n    time: "02:00:00"\n',
    )
    _add_suite_compile(minimal_project, 'compile:\n  time: "03:00:00"\n')
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    build_advice = {a["resource"]: a for a in advice if a["phase"] == "compile"}

    time_a = build_advice["time"]
    # The suite's 3 h, not cfg-dispatch's 2 h, is what was reserved...
    assert time_a["reserved"] == "03:00:00"
    # ...and it is the suite's tests.yaml the project is sent to edit.
    assert time_a["edit_hint"]["path"] == "compile.time"
    assert time_a["edit_hint"]["file"].endswith("tests.yaml")

    # cpus came from cfg-dispatch, so its row still names the root config.
    cpus_a = build_advice["cpus"]
    assert cpus_a["edit_hint"]["path"] == "cfg-dispatch.compile.cpus"
    assert cpus_a["edit_hint"]["file"].endswith("root_config.yaml")


def test_a_build_job_that_only_reused_stamps_gets_no_reduce_advice(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The re-run trap the whole feature could otherwise walk into (#495).

    Re-dispatch an unchanged suite (the normal case after a flaky sim) and
    every build short-circuits on its stamp: the build job is seconds long
    against its 2 h limit with near-zero cpu time. sacct alone cannot tell
    that from a fast compile, so without the envelope's ``reused`` flags
    the advice would be "reduce time → 00:05:00" — which the next real RTL
    change TIMEOUTs against, and afterok then cancels the sim fan-out.
    """
    _mark_stub_builder_verilator(minimal_project)
    _build_telemetry_backend(
        monkeypatch,
        builds=[
            {
                "test": "basic",
                "builder": "verilator",
                "duration_sec": 0.0,
                "reused": True,
                "group": "obj_dir_cafe",
            }
        ],
        build_telemetry={
            "state": "COMPLETED",
            "elapsed_s": 12,
            "timelimit_s": 7200,
            "alloc_cpus": 8,
            "total_cpu_s": 3,
        },
    )
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "01:00:00"\n'
        '  compile:\n    cpus: 4\n    mem: 8G\n    time: "02:00:00"\n'
        "    parallel: 2\n",
    )
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    assert [a for a in advice if a["phase"] == "compile"] == []


def test_no_build_reservation_advice_without_build_telemetry(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """A backend that reports no usage gets no build advice (#495).

    local-parallel is exactly that backend: `collect_telemetry` returns
    ``{}``, and inventing a reservation verdict from nothing would be the
    one kind of wrong that costs a compile an OOM kill.
    """
    _mark_stub_builder_verilator(minimal_project)
    _build_telemetry_backend(monkeypatch, build_telemetry=None)
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    assert [a for a in advice if a["phase"] == "compile"] == []


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
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
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
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
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
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
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


@pytest.mark.parametrize("backend", ["slurm", "local-parallel"])
def test_rebuild_goes_to_the_build_job_and_not_to_its_gated_elements(
    backend: str,
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    fake_backend: _FakeBackend,
):
    """The head rule for ``--rebuild`` under dispatch (#494).

    The build job is the single writer of the shared directory, so it is
    the one place a forced recompile costs one compile instead of one per
    element. Handing the gated array ``--rebuild`` as well would defeat the
    fresh stamp that is exactly what stops the elements compiling, and put
    every one of them into that directory at once (#369).

    ``local-parallel`` is the same rule and, deliberately, the same head
    code: its jobs are separate PROCESSES gated on the build's result just
    as Slurm's array elements are, so the per-process rebuild memo does not
    cover them either. Parametrised rather than written twice so that the
    duplication is visible as duplication — if the head ever grows a
    backend-specific branch, this is where it gets caught.
    """
    result, _ = _invoke(["randtest", "basic", "3", "--dispatch", backend, "--rebuild"])
    assert result.exit_code == 0, result.output

    assert len(fake_backend.build_submitted) == 1
    assert fake_backend.build_submitted[0].rebuild is True
    assert not any(spec.rebuild for spec in fake_backend.submitted)


def test_rebuild_goes_to_the_sim_jobs_when_no_build_job_was_submitted(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """With no build job every element owns its own per-test build dir, so
    rebuilding there races nothing — and it is the only place the request
    can be honoured at all."""
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "--dispatch", "slurm", "--rebuild"]
    )
    assert result.exit_code == 0, result.output

    assert fake_backend.build_submitted == []
    assert fake_backend.submitted
    assert all(spec.rebuild for spec in fake_backend.submitted)


def test_a_suite_that_did_not_ask_carries_no_rebuild_anywhere(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    fake_backend: _FakeBackend,
):
    """Byte-parity at the default: the specs are a pre-#494 head's."""
    result, _ = _invoke(["randtest", "basic", "3", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    assert not fake_backend.build_submitted[0].rebuild
    assert not any(spec.rebuild for spec in fake_backend.submitted)


def test_a_retry_carries_rebuild_but_never_adds_it(minimal_project: Path):
    """A gated element denied ``--rebuild`` on its first attempt must not
    acquire it on its second, and one that legitimately holds it (its own
    per-test dir) still needs it if the attempt died before compiling."""
    rb = RtlBuddy(name="retry_rebuild")
    gated = SimJobSpec(
        test_name="alpha",
        suite_dir=".",
        test_config_path="tests.yaml",
        result_json=Path("r.json"),
        expect_prebuilt=True,
    )
    assert rb._retry_spec(gated, attempt=2).rebuild is False
    ungated = replace(gated, expect_prebuilt=False, rebuild=True)
    assert rb._retry_spec(ungated, attempt=2).rebuild is True


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

    def __init__(
        self,
        *,
        banner=True,
        passes_on_attempt=None,
        state="TIMEOUT",
        capture=True,
        build_result=True,
        cpu_telemetry=None,
    ):
        super().__init__(write_results=False)
        # Reserved-vs-used numbers folded into every job's row, so a retry
        # fleet can also exercise reservation advice (#505 review).
        self.cpu_telemetry = cpu_telemetry or {}
        self.banner = banner
        self.passes_on_attempt = passes_on_attempt
        self.state = state
        # `capture` False: the job never ran, so it wrote no output at all
        # — the shape of a sim whose build job failed underneath it.
        self.capture = capture
        # A real `rb _build-job` always writes its result file (that is how
        # the head maps a compile failure to a CompileFail); `build_result`
        # False is the build job that died before writing one.
        self.build_result = build_result
        self.attempts: dict[str, int] = {}
        self.delays: list[float] = []
        self.log_paths: list = []
        self.states: dict[str, str] = {}
        self.wait_calls = 0

    def submit_build(self, spec):
        # A real build job always writes its result file; the head now takes
        # its absence as "the gate never opened" (#405 review).
        if self.build_result:
            write_build_result_json(spec.result_json, built=[], failed=[])
        return super().submit_build(spec)

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
        if self.capture:
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

    def wait_all(self, handles, *, extra_wait=0.0):
        self.wait_calls += 1
        super().wait_all(handles, extra_wait=extra_wait)

    def collect_telemetry(self, handles):
        self.telemetry_queries.append([h.job_id for h in handles])
        return {
            h.job_id: {**self.cpu_telemetry, "state": self.states.get(h.job_id)}
            for h in handles
        }


def _backend_factory(backend):
    """Stand in for `create_dispatch_backend`, the way a real one behaves.

    `SlurmDispatchBackend.__init__` keeps the `sbatch_args` it was built
    with, and right-sizing reads a job's cpu-request overrides off the
    backend rather than off whichever `cfg-dispatch` is current (#505
    review). A fake that ignores `cfg` would hide exactly that wiring.
    """

    def factory(name, cfg):
        backend.effective_sbatch_args = list(getattr(cfg, "sbatch_args", None) or [])
        return backend if name not in (None, "local") else None

    return factory


def _use_backend(monkeypatch: pytest.MonkeyPatch, backend):
    monkeypatch.setattr(
        rtl_buddy_module,
        "create_dispatch_backend",
        _backend_factory(backend),
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
        + "    classifiers: [license-queue]\n"
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
    # No attempt count anywhere in the row: with retry off there is nothing
    # to count, and "after N attempts" must not appear.
    desc = _rows(result)["basic"]["desc"]
    assert "attempt" not in desc, desc


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


class _PoolRetryBackend(_RetryBackend):
    """A backend shaped exactly like ``local-parallel``: no scheduler at all.

    ``scheduled`` is False and ``collect_telemetry`` is empty by design, so
    there is no scheduler state for the classifier to read. If retry
    required one, it could never fire here (#405 review).
    """

    name = "fake-pool"
    scheduled = False

    def collect_telemetry(self, handles):
        return {}


def test_retry_fires_on_a_backend_with_no_scheduler_state(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _enable_retry(minimal_project)
    backend = _use_backend(monkeypatch, _PoolRetryBackend(passes_on_attempt=2))

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
    assert result.exit_code == 0, result.output
    assert _rows(result)["basic"]["result"] == "PASS"
    assert [spec.test_name for spec in backend.submitted] == ["basic", "basic"]
    assert backend.delays == [0.0, 5.0]


def test_a_pool_backend_still_needs_the_banner_to_retry(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """No scheduler state to require does not mean no evidence required."""
    _enable_retry(minimal_project)
    backend = _use_backend(monkeypatch, _PoolRetryBackend(banner=False))

    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "--dispatch", "local-parallel"]
    )
    assert result.exit_code == 1, result.output
    assert [spec.test_name for spec in backend.submitted] == ["basic"]


def test_the_retry_wait_allows_for_the_backoff_it_imposed(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """max-wait must not be spent on the hold the head itself asked for.

    A held job is outstanding for the whole backoff, so the retry round's
    deadline is widened by that delay; otherwise a max-wait shorter than
    the backoff would trip on every retry before the job could start.
    """
    _enable_retry(minimal_project, attempts=1, backoff=5, cap=20)
    backend = _use_backend(monkeypatch, _RetryBackend(passes_on_attempt=2))

    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    # The first wait carries no allowance; the retry wait carries its delay.
    assert backend.extra_waits == [0.0, 5.0]


def test_a_job_whose_build_job_never_succeeded_is_not_retried(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """It never launched, so there is no attempt to retry (#405 review).

    The build job dies without writing its result; every sim it gated is
    cancelled (`afterok`) or skipped by the pool and writes nothing at all.
    A banner left in `artefacts/basic/test.log` by an earlier run must not
    make the head resubmit that sim — which it would do *ungated*, running
    a job the head deliberately skipped.
    """
    _mark_stub_builder_verilator(minimal_project)  # so a build job is submitted
    _enable_retry(minimal_project)
    backend = _use_backend(
        monkeypatch, _PoolRetryBackend(capture=False, build_result=False)
    )
    artefacts = minimal_project / "artefacts" / "basic"
    artefacts.mkdir(parents=True, exist_ok=True)
    (artefacts / "test.log").write_text(LICENSE_BANNER)

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
    assert _rows(result)["basic"]["result"] == "FAIL"
    assert [spec.test_name for spec in backend.submitted] == ["basic"]
    # ...and the one submission there was kept its build gate.
    assert backend.dependencies == ["fake-build"]


def test_a_stale_capture_from_an_earlier_run_is_not_evidence(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """`artefacts/<test>/test.log` is never cleaned between runs.

    This attempt wrote nothing; the banner on disk predates its
    submission, so it is a previous run's and cannot justify a retry.
    """
    _enable_retry(minimal_project)
    backend = _use_backend(monkeypatch, _RetryBackend(capture=False))
    artefacts = minimal_project / "artefacts" / "basic"
    artefacts.mkdir(parents=True, exist_ok=True)
    stale = artefacts / "test.log"
    stale.write_text(LICENSE_BANNER)
    two_days_ago = time.time() - 2 * 86400
    os.utime(stale, (two_days_ago, two_days_ago))

    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 1, result.output
    assert _rows(result)["basic"]["result"] == "FAIL"
    assert [spec.test_name for spec in backend.submitted] == ["basic"]


def test_a_sim_that_got_its_seat_and_then_hung_is_not_retried(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Banner, then real simulator output, then the reservation ran out.

    The common case, not a corner: most sims that print the banner do get a
    seat. Retrying this one would re-run a genuine hang.
    """
    _enable_retry(minimal_project)

    class _SeatGrantedThenHung(_RetryBackend):
        def submit(self, spec, *, dependency=None, delay_sec=0.0):
            handle = super().submit(spec, dependency=dependency, delay_sec=delay_sec)
            artefacts = Path(spec.suite_dir) / "artefacts" / spec.test_name
            (artefacts / "test.log").write_text(
                LICENSE_BANNER + "....\nVCS Simulation Report\nrunning...\n"
            )
            return handle

    backend = _use_backend(monkeypatch, _SeatGrantedThenHung())

    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 1, result.output
    assert _rows(result)["basic"]["result"] == "FAIL"
    assert [spec.test_name for spec in backend.submitted] == ["basic"]
    assert "retrying basic" not in result.output


class _UnsubmittableRetryBackend(_RetryBackend):
    """Accepts the first fan-out, refuses every retry — a flaky ``sbatch``."""

    def submit(self, spec, *, dependency=None, delay_sec=0.0):
        if delay_sec:
            raise FatalRtlBuddyError("sbatch: error: Batch job submission failed")
        return super().submit(spec, dependency=dependency, delay_sec=delay_sec)


def test_a_failed_resubmission_keeps_the_results_already_collected(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A retry is a second chance, never a way to lose a scored regression.

    The rows for the pass are already written and already say the job
    produced no result, so a refusing scheduler degrades to those rather
    than aborting the command with no summary and no machine payload.
    """
    _enable_retry(minimal_project)
    backend = _use_backend(monkeypatch, _UnsubmittableRetryBackend())

    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 1, result.output
    # The payload still exists, and the row is the honest one.
    assert _rows(result)["basic"]["result"] == "FAIL"
    assert backend.cancelled  # this attempt's jobs were taken down


def test_an_abandoned_retry_says_so_on_the_console(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _enable_retry(minimal_project)
    _use_backend(monkeypatch, _UnsubmittableRetryBackend())

    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 1, result.output
    assert "giving up on retry attempt 1" in result.output
    assert "keeping the results already collected" in result.output


def test_a_head_side_bug_in_the_retry_path_is_not_swallowed(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The degrade-to-collected contract covers cluster weather, not bugs.

    A refusing scheduler is survivable; a TypeError from rtl-buddy's own
    head-side code is a defect, and burying it in `dispatch.retry_abandoned`
    would hide the whole feature failing to launch anything.
    """
    _enable_retry(minimal_project)

    class _BuggyRetryBackend(_RetryBackend):
        def submit(self, spec, *, dependency=None, delay_sec=0.0):
            if delay_sec:
                raise TypeError("submit() got an unexpected keyword argument")
            return super().submit(spec, dependency=dependency, delay_sec=delay_sec)

    _use_backend(monkeypatch, _BuggyRetryBackend())

    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert isinstance(result.exception, TypeError), result.output
    assert "giving up on retry attempt" not in result.output


# ------------------------------------------- #440: single-test dispatch


def test_single_test_dispatch_submits_one_build_and_one_gated_job(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """`rb test <name> --dispatch` is the regression plan narrowed to one test.

    The point of #440: one build job, one sim job gated on it, and the
    result collected from the job's envelope — no new machinery, and no
    throwaway one-suite reg_config to author.
    """
    _mark_stub_builder_verilator(minimal_project)
    result, rb = _invoke(["test", "basic", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    assert [spec.test_name for spec in fake_backend.submitted] == ["basic"]
    assert len(fake_backend.build_submitted) == 1
    assert fake_backend.dependencies == ["fake-build"]
    assert fake_backend.waited
    assert not fake_backend.cancelled

    # Dispatch implies share_build; the sim job carries a reservation and
    # a single unnumbered run.
    assert rb.share_build is True
    (spec,) = fake_backend.submitted
    assert spec.share_build is True
    assert spec.run_id is None
    assert spec.resources.time is not None
    assert spec.result_json.is_file()
    # ...and the run was scored from that envelope, not from a local run.
    assert "PASS" in result.output


def test_multiple_test_dispatch_uses_one_build_and_selected_jobs(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    _mark_stub_builder_verilator(minimal_project)
    result, rb = _invoke(["test", "extra", "basic", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    assert [spec.test_name for spec in fake_backend.submitted] == ["extra", "basic"]
    assert len(fake_backend.build_submitted) == 1
    assert fake_backend.dependencies == ["fake-build", "fake-build"]
    assert rb.share_build is True
    assert [
        cfg.get_name() for cfg in read_plan_configs(fake_backend.submitted[0].plan_path)
    ] == [
        "extra",
        "basic",
    ]


def test_single_test_dispatch_keeps_the_test_commands_builder_mode(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """`rb test` defaults the builder mode to `debug`, `rb regression` to
    `reg`. Dispatch must carry the *command's* default into its jobs, or a
    dispatched `rb test` would compile with different opts than a local one.
    """
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(["test", "basic", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    assert fake_backend.submitted[0].builder_mode == "debug"
    assert fake_backend.build_submitted[0].builder_mode == "debug"


def test_single_test_dispatch_narrows_the_plan_to_the_named_test(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """`rb test` applies no level filter, so an un-narrowed plan would sweep
    the whole suite — the exact cost the issue calls out."""
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(["test", "extra", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    assert [spec.test_name for spec in fake_backend.submitted] == ["extra"]
    (spec,) = fake_backend.submitted
    assert [cfg.get_name() for cfg in read_plan_configs(spec.plan_path)] == ["extra"]


def test_unnamed_test_dispatch_covers_the_suite(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """No test name selects the suite, dispatched or not — the same
    selection the in-process path makes."""
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(["test", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    assert [spec.test_name for spec in fake_backend.submitted] == ["basic", "extra"]


def test_single_test_dispatch_respects_levels_and_share_build(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """The level options `rb test` already carries compose with dispatch."""
    _mark_stub_builder_verilator(minimal_project)
    result, rb = _invoke(
        ["test", "--reg-level", "0", "--share-build", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    # "extra" is reglvl 5 — filtered out before the plan, as in-process.
    assert [spec.test_name for spec in fake_backend.submitted] == ["basic"]
    assert rb.share_build is True


def test_test_without_dispatch_keeps_the_in_process_path(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    fake_backend: _FakeBackend,
):
    """No `--dispatch`, no `cfg-dispatch`: byte-identical to before #440 —
    nothing is submitted and the stubbed TestRunner runs in-process."""
    stub_build_runner.canned = TestPassResults(name="basic/results")
    result, rb = _invoke(["test", "basic"])
    assert result.exit_code == 0, result.output
    assert fake_backend.submitted == []
    assert fake_backend.build_submitted == []
    assert stub_build_runner.inits, "expected an in-process TestRunner"
    assert rb.share_build is False


def test_explicit_dispatch_local_keeps_the_in_process_path(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    fake_backend: _FakeBackend,
):
    stub_build_runner.canned = TestPassResults(name="basic/results")
    result, _ = _invoke(["test", "basic", "--dispatch", "local"])
    assert result.exit_code == 0, result.output
    assert fake_backend.submitted == []


def test_single_test_dispatch_missing_result_is_dispatch_fail(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    fake_backend.write_results = False
    result, _ = _invoke(["--machine", "test", "basic", "--dispatch", "slurm"])
    assert result.exit_code == 1, result.output
    row = _rows(result)["basic"]
    assert row["result"] == "FAIL"
    assert "produced no result" in row["desc"]


def test_single_test_dispatch_rejects_early_stop(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A stop before POST is not expressible per job, on `rb test` either."""
    result, _ = _invoke(
        ["--early-stop", "comp", "test", "basic", "--dispatch", "slurm"]
    )
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "cannot be combined with dispatch (--dispatch slurm)" in str(
        result.exception
    )
    assert "run without --dispatch" in str(result.exception)
    assert fake_backend.submitted == []


def test_jobs_on_test_is_validated_against_the_backend(minimal_project: Path):
    """`-j` must mean something on `rb test` too, or be rejected (#360)."""
    result, _ = _invoke(["test", "basic", "--dispatch", "slurm", "-j", "4"])
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "max-jobs-per-array" in str(result.exception)


def test_jobs_on_test_without_dispatch_is_rejected_not_dropped(
    minimal_project: Path,
):
    """`rb test` never reads `cfg-dispatch.backend`, so a bare `-j` sizes a
    pool that will not exist — reject it rather than drop it (#360)."""
    result, _ = _invoke(["test", "basic", "-j", "4"])
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "the backend is local" in str(result.exception)


def test_dispatch_flags_are_validated_before_the_list_short_circuit(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """`--list` exits without running anything, so a dispatch flag beside it
    is unusable — and an unusable flag is rejected, never dropped (#360)."""
    result, _ = _invoke(["test", "--list", "-j", "4"])
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "the backend is local" in str(result.exception)

    result, _ = _invoke(["test", "--list", "--dispatch", "slurm"])
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "--list cannot be combined with --dispatch slurm" in str(result.exception)
    assert fake_backend.submitted == []

    # `--dispatch local` is the in-process default spelled out: no conflict.
    result, _ = _invoke(["test", "--list", "--dispatch", "local"])
    assert result.exit_code == 0, result.output
    assert "basic" in result.output


def test_an_unknown_backend_is_rejected_before_the_list_message(
    minimal_project: Path,
):
    """A typo'd backend must not be quoted back as though it existed.

    `--list cannot be combined with --dispatch slrum` vouches for `slrum`;
    the name is checked first so the answer names the real choices
    (#440 review).
    """
    result, _ = _invoke(["test", "--list", "--dispatch", "slrum"])
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    message = str(result.exception)
    assert "unknown dispatch backend 'slrum'" in message
    assert "--list cannot be combined" not in message


def test_cfg_dispatch_backend_does_not_apply_to_test(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    fake_backend: _FakeBackend,
):
    """Dispatching `rb test` is opt-in per invocation (#440 review).

    `rb regression` and `rb randtest` default their backend from
    `cfg-dispatch.backend`; `rb test` deliberately does not. It is the local
    iteration command, and a project that set `backend: slurm` for its
    regressions must not find single-test runs queueing after an upgrade —
    nor be told to drop a `--dispatch` flag it never passed.
    """
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(root_cfg.read_text() + "\ncfg-dispatch:\n  backend: slurm\n")
    _mark_stub_builder_verilator(minimal_project)
    stub_build_runner.canned = TestPassResults(name="basic/results")

    result, rb = _invoke(["test", "basic"])
    assert result.exit_code == 0, result.output
    assert fake_backend.submitted == []
    assert fake_backend.build_submitted == []
    assert stub_build_runner.inits, "expected an in-process TestRunner"
    assert rb.share_build is False


def test_cfg_dispatch_backend_leaves_early_stop_on_test_alone(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    fake_backend: _FakeBackend,
):
    """The corollary: `rb test --early-stop` keeps working under a project
    that configured a cluster backend, instead of failing with advice to
    drop a `--dispatch` flag the command line never carried."""
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(root_cfg.read_text() + "\ncfg-dispatch:\n  backend: slurm\n")
    result, _ = _invoke(["--early-stop", "comp", "test", "basic"])
    assert result.exit_code == 0, result.output
    assert fake_backend.submitted == []


def test_cfg_dispatch_settings_still_configure_an_opted_in_test_run(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """Only `backend` is ignored: once `--dispatch` opts in, the rest of the
    `cfg-dispatch` block configures the run as it does everywhere else."""
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        "  backend: slurm\n"
        '  resources:\n    cpus: 3\n    mem: 7G\n    time: "00:20:00"\n',
    )
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(["test", "basic", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    (spec,) = fake_backend.submitted
    assert (spec.resources.cpus, spec.resources.mem) == (3, "7G")


def test_cfg_dispatch_backend_early_stop_error_names_the_config(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """On the commands that *do* read the config, the rejection has to name
    it: "run without --dispatch" is unactionable when no flag was passed."""
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(root_cfg.read_text() + "\ncfg-dispatch:\n  backend: slurm\n")
    result, _ = _invoke(["--early-stop", "comp", "regression", "-c", "regression.yaml"])
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "cfg-dispatch.backend: fake" in str(result.exception)
    assert "pass --dispatch local" in str(result.exception)
    assert fake_backend.submitted == []


def test_single_test_dispatch_without_a_shareable_builder_has_no_build_job(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """One test whose builder cannot share a build gets no build job (#358).

    `rb test` plans one row per entry, so nothing fans out in-job: the lone
    sim job compiles inside its own allocation and is ungated — the shape
    the "one build job, one gated sim job" summary does *not* describe.
    """
    # No `_mark_stub_builder_verilator`: the fixture's inferred "echo"
    # family has no shared-build support.
    result, _ = _invoke(["test", "basic", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    assert fake_backend.build_submitted == []
    assert [spec.test_name for spec in fake_backend.submitted] == ["basic"]
    assert fake_backend.dependencies == [None]


@pytest.mark.parametrize(
    "flag, expected_mode",
    [("-n", SeedMode.NEW), ("-l", SeedMode.REPLAY)],
)
def test_seed_selection_travels_to_the_dispatched_job(
    minimal_project: Path,
    fake_backend: _FakeBackend,
    flag: str,
    expected_mode: SeedMode,
):
    """`-n` / `-l` are `rb test` options that only the job can act on, so
    they have to reach it as its `--seed-mode` (documented contract)."""
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(["test", "basic", flag, "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    (spec,) = fake_backend.submitted
    assert spec.seed_mode == expected_mode
    # One unnumbered run either way: `rb test` never fans out over seeds.
    assert spec.run_id is None
    assert spec.replay_run_id is None


def test_an_interrupted_single_test_wait_cancels_its_jobs(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Ctrl-C on the head must not leave the build and sim jobs running
    after it exits and releases the tree lock (#361)."""

    class _InterruptBackend(_FakeBackend):
        def wait_all(self, handles, *, extra_wait=0.0):
            self.waited = True
            raise KeyboardInterrupt

        def cancel_all(self, handles):
            self.cancelled = [handle.job_id for handle in handles]

    backend = _InterruptBackend()
    _use_backend(monkeypatch, backend)
    _mark_stub_builder_verilator(minimal_project)

    result, _ = _invoke(["test", "basic", "--dispatch", "slurm"])
    # The interrupt is reported as the conventional 128+SIGINT exit...
    assert result.exit_code == 130, result.output
    # ...and both jobs were cancelled on the way out, build included.
    assert backend.cancelled == ["fake-build", "fake-1"]


def test_single_test_dispatch_announces_its_job_before_waiting(
    minimal_project: Path,
    fake_backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
):
    order = []
    _spy_on_console_events(monkeypatch, order)
    _spy_on_wait(monkeypatch, fake_backend, order)
    _mark_stub_builder_verilator(minimal_project)

    result, _ = _invoke(["test", "basic", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    names = [event for event, _ in order]
    assert names.index("dispatch.suite_submitted") < names.index("wait_all")
    (fields,) = [f for event, f in order if event == "dispatch.suite_submitted"]
    assert fields["job_ids"] == ["fake-1"]
    assert fields["build_job"] == "fake-build"
    assert fields["jobs"] == 2
    assert fields["suite"] == "tests.yaml"


def test_single_test_machine_payload_carries_reservation_advice(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    backend = _RecordingBackend(
        telemetry={
            "fake-1": {"state": "COMPLETED", "elapsed_s": 15, "timelimit_s": 3600}
        }
    )
    _use_backend(monkeypatch, backend)
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(["--machine", "test", "basic", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    (time_a,) = [a for a in advice if a["resource"] == "time"]
    assert time_a["direction"] == "reduce"


def test_non_dispatched_test_payload_has_no_reservation_advice(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
):
    """The key is absent, not empty, when nothing was dispatched — same
    contract the regression payload keeps."""
    stub_build_runner.canned = TestPassResults(name="basic/results")
    result, _ = _invoke(["--machine", "test", "basic"])
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    assert "reservation_advice" not in json.loads(payload_line)["payload"]


def test_a_retry_re_snapshots_the_cpu_overrides_it_was_submitted_with(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A retry is a fresh sbatch from a possibly different environment.

    The first attempt went out with nothing overriding cpus, so the row
    recorded the resolved 1 as the request. Between then and the retry a
    later suite's in-process sweep hook exported `SBATCH_NTASKS=4`, and
    `_resubmit_retryable` submits into that environment — so the retry
    really did ask for four cpus, and it is the retry's telemetry the
    analysis reads. Keeping the first attempt's metadata would judge it
    against a request of 1, which the `cpus > 1` guard drops, silently
    losing valid advice (#505 review).

    The first `wait_all` is that window: it runs after the first attempt is
    submitted and before it is collected and resubmitted.
    """
    _enable_retry(minimal_project)
    backend = _use_backend(
        monkeypatch,
        _RetryBackend(
            passes_on_attempt=2,
            cpu_telemetry={
                "elapsed_s": 100,
                "timelimit_s": 3600,
                "alloc_cpus": 4,
                "req_cpus": 4,  # 4 tasks x the generated 1 cpu
                "total_cpu_s": 100.0,  # 0.25 efficiency against those 4
            },
        ),
    )

    real_wait_all = backend.wait_all

    def wait_all_then_export(handles, **kwargs):
        os.environ["SBATCH_NTASKS"] = "4"
        return real_wait_all(handles, **kwargs)

    monkeypatch.setattr(backend, "wait_all", wait_all_then_export)
    monkeypatch.delenv("SBATCH_NTASKS", raising=False)

    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    # The retry really was submitted, and into the changed environment.
    assert [spec.test_name for spec in backend.submitted] == ["basic", "basic"]

    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    (cpus,) = [a for a in advice if a["resource"] == "cpus"]
    assert cpus["reserved"] == "4"  # the retry's request, not the first attempt's 1
    assert cpus["edit_hint"]["path"] == "env"
    assert (
        "`SBATCH_NTASKS=4` multiplies this job's cpu request"
        in (cpus["edit_hint"]["note"])
    )


@pytest.mark.parametrize("fail_at", ["submit", "wait"])
def test_an_abandoned_retry_leaves_the_first_attempts_cpu_metadata(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_at: str,
):
    """Metadata must describe the attempt whose telemetry sits beside it.

    The ambient `SBATCH_NTASKS` changes before the retry, but the retry is
    then refused by `sbatch` — or its wait fails. The head deliberately
    keeps the first attempt's results and telemetry after those cluster
    failures, so the row must keep the first attempt's reservation too.
    Rewriting it up front paired that telemetry with an attempt that never
    ran, picking the wrong cpu denominator and naming an override that was
    never in force for it (#505 review).

    With the first attempt's metadata the resolved request is 1 cpu, which
    the `cpus > 1` guard drops — so a cpus row appearing at all is the bug.
    """
    _enable_retry(minimal_project)
    backend = _use_backend(
        monkeypatch,
        _RetryBackend(
            cpu_telemetry={
                "elapsed_s": 100,
                "timelimit_s": 3600,
                "alloc_cpus": 4,
                "req_cpus": 4,
                "total_cpu_s": 100.0,  # 0.25 efficiency
            },
        ),
    )

    real_wait_all = backend.wait_all
    real_submit = backend.submit

    def wait_all_then_export(handles, **kwargs):
        # The window a later suite's in-process sweep hook would run in.
        os.environ["SBATCH_NTASKS"] = "4"
        if fail_at == "wait" and backend.wait_calls >= 1:
            raise FatalRtlBuddyError("max-wait elapsed on the retry round")
        return real_wait_all(handles, **kwargs)

    def submit_or_refuse(spec, **kwargs):
        if fail_at == "submit" and backend.attempts.get(spec.test_name):
            raise FatalRtlBuddyError("sbatch: error: QOSMaxSubmitJobPerUserLimit")
        return real_submit(spec, **kwargs)

    monkeypatch.setattr(backend, "wait_all", wait_all_then_export)
    monkeypatch.setattr(backend, "submit", submit_or_refuse)
    monkeypatch.delenv("SBATCH_NTASKS", raising=False)

    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    # The retry was abandoned, not the run: the first attempt's rows stand.
    assert "retry_abandoned" in result.output or "abandoned" in result.output

    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    # The first attempt asked for the resolved 1 cpu with nothing overriding
    # it, and a one-cpu reservation has no cpus advice to give.
    assert [a for a in advice if a["resource"] == "cpus"] == []


def _use_backend_with_fixed_args(monkeypatch, backend, sbatch_args):
    """A backend built from a DIFFERENT config than the suite's.

    The real shape: `_resolve_dispatch_backend` runs once, before the suite
    loop, off the orchestration `root_config.yaml`; `root_cfg` is then
    rebuilt for any suite that walks up to a different one. The backend
    keeps the arguments it was constructed with, whatever the current
    suite's `cfg-dispatch` says.
    """

    def factory(name, cfg):
        backend.effective_sbatch_args = list(sbatch_args)
        return backend if name not in (None, "local") else None

    monkeypatch.setattr(rtl_buddy_module, "create_dispatch_backend", factory)
    return backend


def test_an_override_only_the_backend_carries_is_still_found(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The suite's `cfg-dispatch` is not what `sbatch` received.

    In a multi-root regression the backend is built once from the
    orchestration config while `root_cfg` is rebuilt per suite, so the
    suite's `sbatch-args` can be empty while the backend really appends
    `--ntasks=4`. Scanning the suite's config misses that override, records
    the generated per-task cpus as the whole-job request, and the `cpus > 1`
    guard then drops advice the run genuinely deserved (#505 review).
    """
    backend = _use_backend_with_fixed_args(
        monkeypatch,
        _RecordingBackend(
            telemetry={
                "fake-1": {
                    "state": "COMPLETED",
                    "elapsed_s": 100,
                    "timelimit_s": 3600,
                    "req_mem_bytes": 8 * 2**30,
                    "alloc_cpus": 4,
                    "req_cpus": 4,  # 4 tasks x the generated 1 cpu
                    "total_cpu_s": 100.0,  # 0.25 efficiency against those 4
                }
            }
        ),
        ["--ntasks=4"],
    )
    assert backend is not None
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    (cpus,) = [a for a in advice if a["resource"] == "cpus"]
    assert cpus["reserved"] == "4"
    assert cpus["edit_hint"]["path"] == "cfg-dispatch.sbatch-args"
    assert (
        "`--ntasks=4` multiplies this job's cpu request" in (cpus["edit_hint"]["note"])
    )


def test_a_suite_override_the_backend_never_had_makes_no_false_hint(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The mirror case: the suite's config claims what sbatch never got.

    Reading it would name `sbatch-args` as the thing to edit for a run
    submitted without it — an edit hint pointing at an argument that was
    never in force, which is the unappliable advice #505 exists to remove.
    """
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text()
        + "\ncfg-dispatch:\n"
        + "  resources: {cpus: 4}\n"
        + "  sbatch-args: [--ntasks=4]\n"
    )
    _use_backend_with_fixed_args(
        monkeypatch,
        _RecordingBackend(
            telemetry={
                "fake-1": {
                    "state": "COMPLETED",
                    "elapsed_s": 100,
                    "timelimit_s": 3600,
                    "req_mem_bytes": 8 * 2**30,
                    "alloc_cpus": 4,
                    "req_cpus": 4,  # just the generated 4; no task multiplier
                    "total_cpu_s": 100.0,  # 0.25 efficiency
                }
            }
        ),
        [],  # ...but this backend appends nothing
    )
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    (cpus,) = [a for a in advice if a["resource"] == "cpus"]
    # The YAML field really does govern this run, so that is what to edit.
    assert cpus["edit_hint"]["path"] == "tests[name=basic].resources.cpus"
    assert "note" not in cpus["edit_hint"]


# ------------------- the shared-binary audit (#535)


def _stamped_row(test_name, sha, simv):
    results = TestPassResults(name=f"{test_name}/results")
    results.results["build_stamp"] = {"fingerprint_sha": sha, "simv": simv}
    return {"test_name": test_name, "randmode_i": None, "results": results}


def test_the_collect_audit_warns_when_one_key_produced_two_binaries(caplog):
    """One compile key is one binary; anything else is a substitution (#535).

    Every run gated on a build job validated the same stamp, so agreeing
    digests with disagreeing executables mean somebody rebuilt the shared
    directory while its neighbours were reusing it. Reporting only — the
    runs are already scored against whatever they ran, and the point is
    that the substitution stops being invisible.
    """
    import logging as _logging

    rows = [
        _stamped_row("alpha", "k1", ["/b/simv", 10, 1]),
        _stamped_row("beta", "k1", ["/b/simv", 11, 2]),
        _stamped_row("gamma", "k2", ["/c/simv", 10, 1]),
        # Nothing to say: a run with no shared build at all.
        {
            "test_name": "delta",
            "randmode_i": None,
            "results": TestPassResults(name="delta/results"),
        },
    ]
    with caplog.at_level(_logging.WARNING):
        RtlBuddy._audit_shared_binaries(rows)

    events = [
        record.rtl_fields
        for record in caplog.records
        if getattr(record, "rtl_event", None) == "dispatch.binary_mismatch"
    ]
    assert len(events) == 1
    assert events[0]["fingerprint_sha"] == "k1"
    assert events[0]["binaries"] == 2
    assert events[0]["tests"] == ["alpha", "beta"]


def test_the_collect_audit_is_silent_when_every_run_named_one_binary(caplog):
    """The healthy fan-out must say nothing, or the warning is worthless."""
    import logging as _logging

    rows = [
        _stamped_row("alpha", "k1", ["/b/simv", 10, 1]),
        _stamped_row("beta", "k1", ["/b/simv", 10, 1]),
    ]
    with caplog.at_level(_logging.WARNING):
        RtlBuddy._audit_shared_binaries(rows)
    assert not [
        record
        for record in caplog.records
        if getattr(record, "rtl_event", None) == "dispatch.binary_mismatch"
    ]
