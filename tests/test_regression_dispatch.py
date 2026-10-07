"""Dispatched regression flow tests.

Exercises ``rb regression --dispatch ...`` over the ``minimal_project`` fixture with a
fake backend and a stubbed ``TestRunner``: head-node build pass, fan-out, collection,
failure mapping and result ordering. No scheduler or simulator is involved.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

import rtl_buddy.rtl_buddy as rtl_buddy_module
from rtl_buddy.config.dispatch import mem_to_bytes, time_to_seconds
from rtl_buddy.dispatch.base import DispatchBackend, JobHandle

# Aliased so pytest does not try to collect the dataclass as a test class.
from rtl_buddy.dispatch.base import TestJobSpec as SimJobSpec
from rtl_buddy.dispatch.plan import (
    read_plan_config,
    read_plan_configs,
    read_plan_master_seed,
    read_plan_token,
)
from rtl_buddy.dispatch.run_manifest import discover_run_manifests
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
from rtl_buddy.seeding import derive_test_seed


class _FakeBackend(DispatchBackend):
    """Records specs and 'runs' each job at submit time by writing, or deliberately not
    writing, its result envelope.
    """

    name = "fake"

    def __init__(self, job_result="PASS", write_results=True):
        self.job_result = job_result
        self.write_results = write_results
        self.submitted = []
        self.build_submitted = []
        self.verilate_submitted = []
        # What each build job was chained behind, in submission order: the verilate
        # job's id for a split compile, else None.
        self.build_dependencies = []
        self.dependencies = []
        self.waited = False
        self.cancelled = False
        self.extra_waits = []
        # Job ids each collect_telemetry pass asked about.
        self.telemetry_queries = []

    def submit_build(self, spec, *, dependency=None):
        # Kept apart from `build_submitted` so that keeps meaning the build job whether
        # or not the compile was split.
        if spec.phase == "verilate":
            self.verilate_submitted.append(spec)
        else:
            self.build_submitted.append(spec)
        self.build_dependencies.append(dependency)
        # `fake-build` names the whole compile and the build half of a split one; the
        # verilate half needs its own id because telemetry keys on it.
        return JobHandle(
            job_id="fake-verilate" if spec.phase == "verilate" else "fake-build",
            spec=spec,
        )

    def submit(self, spec, *, dependency=None, delay_sec=0.0):
        # `delay_sec` is accepted so the fake matches the DispatchBackend ABC.
        self.submitted.append(spec)
        self.dependencies.append(dependency)
        if self.write_results:
            results = (
                TestPassResults(name=spec.test_name + "/results")
                if self.job_result == "PASS"
                else CompileFailResults(name=spec.test_name + "/results")
            )
            if spec.resolved_seed is not None:
                planned_cfg = read_plan_config(spec.plan_path, spec.test_name)
                results.results["seed"] = {
                    "master_seed": spec.master_seed,
                    "resolved_seed": spec.resolved_seed,
                    "source": planned_cfg.seed_source,
                    "identity": planned_cfg.seed_identity,
                }
            # Mirror `rb _test-job`: stamp the head's run token from the plan into the
            # envelope so collection accepts it.
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
    # The compile record VlogSim stamps on itself; the in-process path folds it into
    # every run's result envelope.
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

    The fixture's `builder: "echo"` infers the family `"echo"`, which is neither
    share-build capable nor eligible for time advice (gated to verilator).
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

    # The compile runs as a dispatched build job, never on the head, and the sim is
    # gated on it via afterok.
    assert len(fake_backend.build_submitted) == 1
    assert fake_backend.build_submitted[0].resources.time is not None
    assert fake_backend.dependencies == ["fake-build"]

    # Dispatch implies share_build; sim jobs carry a defined reservation.
    assert rb.share_build is True
    spec = fake_backend.submitted[0]
    assert spec.share_build is True
    assert spec.resources.time is not None
    assert spec.result_json.is_file()


def test_dispatched_regression_carries_master_and_resolved_seed(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(
        [
            "--machine",
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "slurm",
            "--master-seed",
            "20260914",
        ]
    )
    assert result.exit_code == 0, result.output

    spec = fake_backend.submitted[0]
    assert spec.seed_mode == SeedMode.MASTER
    assert spec.master_seed == 20260914
    assert spec.resolved_seed is not None
    assert read_plan_master_seed(spec.plan_path) == 20260914
    planned = read_plan_config(spec.plan_path, spec.test_name)
    assert planned.get_resolved_seed() == spec.resolved_seed

    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    envelope = json.loads(payload_line)
    assert envelope["payload"]["master_seed"] == 20260914


def test_dispatched_master_seed_uses_sweep_expanded_names(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    _mark_stub_builder_verilator(minimal_project)
    (minimal_project / "sweep.py").write_text(
        "import copy\n"
        "out_test_cfgs = []\n"
        "for suffix in ('fp16', 'fp32'):\n"
        "    cfg = copy.deepcopy(test_cfg)\n"
        "    cfg.name = test_cfg.name + '.' + suffix\n"
        "    out_test_cfgs.append(cfg)\n"
    )
    suite_path = minimal_project / "tests.yaml"
    suite_path.write_text(
        suite_path.read_text().replace(
            "    sweep:\n", "    sweep:\n      path: sweep.py\n", 1
        )
    )

    result, _ = _invoke(
        [
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "slurm",
            "--master-seed",
            "20260914",
        ]
    )
    assert result.exit_code == 0, result.output

    specs = {spec.test_name: spec for spec in fake_backend.submitted}
    assert set(specs) == {"basic.fp16", "basic.fp32"}
    for name, spec in specs.items():
        expected = derive_test_seed(
            20260914,
            suite_identity="tests.yaml",
            test_name=name,
            run_id=None,
        )
        assert spec.resolved_seed == expected.seed
        assert (
            read_plan_config(spec.plan_path, name).get_resolved_seed() == expected.seed
        )


def test_direct_test_and_regression_derive_the_same_seed(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    suite_dir = minimal_project / "verif" / "foo"
    suite_dir.mkdir(parents=True)
    for name in ("tests.yaml", "models.yaml"):
        (suite_dir / name).write_text((minimal_project / name).read_text())
    (minimal_project / "regression.yaml").write_text(
        "rtl-buddy-filetype: reg_config\ntest-configs:\n  - verif/foo/tests.yaml\n"
    )

    monkeypatch.chdir(suite_dir)
    direct, direct_rb = _invoke(
        [
            "--machine",
            "test",
            "basic",
            "-c",
            "tests.yaml",
            "--master-seed",
            "20260914",
        ]
    )
    assert direct.exit_code == 0, direct.output
    direct_cfg = stub_build_runner.inits[-1]["test_cfg"]
    direct_seed = direct_cfg.get_resolved_seed()
    direct_rb._artifact_locks.release_all()

    stub_build_runner.inits = []
    monkeypatch.chdir(minimal_project)
    regression, _ = _invoke(
        [
            "--machine",
            "regression",
            "-c",
            "regression.yaml",
            "--master-seed",
            "20260914",
        ]
    )
    assert regression.exit_code == 0, regression.output
    regression_seed = stub_build_runner.inits[-1]["test_cfg"].get_resolved_seed()

    assert direct_seed == regression_seed
    assert direct_cfg.seed_identity == "verif/foo/tests.yaml::basic::single"


def test_same_master_seed_replays_local_regression_seed(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
):
    first, first_rb = _invoke(
        [
            "--machine",
            "regression",
            "-c",
            "regression.yaml",
            "--master-seed",
            "20260914",
        ]
    )
    assert first.exit_code == 0, first.output
    first_seed = stub_build_runner.inits[-1]["test_cfg"].get_resolved_seed()
    first_rb._artifact_locks.release_all()

    stub_build_runner.inits = []
    second, _ = _invoke(
        [
            "--machine",
            "regression",
            "-c",
            "regression.yaml",
            "--master-seed",
            "20260914",
        ]
    )
    assert second.exit_code == 0, second.output
    second_seed = stub_build_runner.inits[-1]["test_cfg"].get_resolved_seed()

    assert first_seed is not None
    assert second_seed == first_seed


def test_test_accepts_master_seed_above_signed_64_bit(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
):
    master_seed = (1 << 96) + 20260914

    result, _ = _invoke(
        ["--machine", "test", "basic", "--master-seed", str(master_seed)]
    )

    assert result.exit_code == 0, result.output
    cfg = stub_build_runner.inits[-1]["test_cfg"]
    assert (
        cfg.get_resolved_seed()
        == derive_test_seed(
            master_seed,
            suite_identity="tests.yaml",
            test_name="basic",
            run_id=None,
        ).seed
    )


@pytest.mark.parametrize("rnd_flag", ["--rnd-new", "--rnd-last"])
def test_test_rejects_master_seed_with_legacy_random_modes(
    minimal_project: Path,
    rnd_flag: str,
):
    result, _ = _invoke(["test", "basic", "--master-seed", "20260914", rnd_flag])

    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "cannot be combined" in str(result.exception)


def test_randtest_rejects_unresolved_preprocessor_seed(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
):
    suite_path = minimal_project / "tests.yaml"
    suite_path.write_text(
        suite_path.read_text().replace(
            "    sim_timeout:\n",
            "    sim_timeout:\n    sim-rand-seed-plusarg: stimulus_seed\n",
            1,
        )
    )

    result, _ = _invoke(["randtest", "basic", "2"])

    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "cannot expose new seeds before preproc" in str(result.exception)
    assert stub_build_runner.inits == []


@pytest.mark.parametrize("seed", [0, 1, -1, 2**31])
def test_default_builder_seed_is_exposed_before_preprocessor(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    seed,
):
    root_path = minimal_project / "root_config.yaml"
    root_path.write_text(
        root_path.read_text().replace("sim-rand-seed: 1", f"sim-rand-seed: {seed}")
    )
    suite_path = minimal_project / "tests.yaml"
    suite_path.write_text(
        suite_path.read_text().replace(
            "    sim_timeout:\n",
            "    sim_timeout:\n    sim-rand-seed-plusarg: stimulus_seed\n",
            1,
        )
    )

    result, _ = _invoke(["test", "basic"])

    assert result.exit_code == 0, result.output
    run_cfg = stub_build_runner.inits[-1]["test_cfg"]
    assert run_cfg.get_resolved_seed() == seed
    assert run_cfg.seed_source == "default"
    assert run_cfg.get_plusarg("stimulus_seed") == seed


def test_randtest_fixed_seed_is_shared_by_preprocessor_and_all_runs(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
):
    suite_path = minimal_project / "tests.yaml"
    suite_path.write_text(
        suite_path.read_text().replace(
            "    sim_timeout:\n",
            "    sim_timeout:\n"
            "    sim-rand-seed: 41\n"
            "    sim-rand-seed-plusarg: stimulus_seed\n",
            1,
        )
    )

    result, _ = _invoke(["--machine", "randtest", "basic", "2"])

    assert result.exit_code == 0, result.output
    run_cfg = stub_build_runner.inits[-1]["test_cfg"]
    assert stub_build_runner.inits[-1]["seed_mode"] == SeedMode.NEW
    assert run_cfg.get_resolved_seed() == 41
    assert run_cfg.get_plusarg("stimulus_seed") == 41
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    rows = json.loads(payload_line)["payload"]["results"]
    assert [
        (row["seed"]["resolved_seed"], row["seed"]["identity"]) for row in rows
    ] == [
        (41, "tests.yaml::basic::single"),
        (41, "tests.yaml::basic::single"),
    ]


def test_master_seed_rejects_one_shared_preprocessor_for_multiple_runs():
    rb = RtlBuddy(name="test_master_seed_multiple")

    with pytest.raises(
        FatalRtlBuddyError, match="requires one run id per expanded test"
    ):
        rb._do_test_suite(
            object(),
            run_ids=[1, 2],
            seed_mode=SeedMode.MASTER,
            master_seed=20260914,
        )


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
    (build_handle=None) and does not orphan other suites' submitted jobs.
    """
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
    # Only the non-empty suite's `basic` reached the fleet; the run drained cleanly
    # rather than cancelling.
    assert [spec.test_name for spec in fake_backend.submitted] == ["basic"]
    assert fake_backend.waited
    assert not fake_backend.cancelled


def test_head_does_not_preunlink_and_rejects_stale_by_token(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """The head does not pre-unlink the result path (on NFS that caches a negative dentry and
    blinds it); a stale envelope is rejected by run_token, so an old PASS never satisfies this run.
    """
    fake_backend.write_results = False  # this run's job leaves no fresh envelope

    # Pre-seed a stale PASS envelope from an earlier run at this run's result path.
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
    # The stale file is still on disk with the old token.
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
    # A build job is submitted and every sim depends on it.
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


def test_dispatch_creates_log_parent_before_submit(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    # The sbatch --output parent must exist at submit time, because slurmstepd opens it
    # before `rb _test-job` runs.
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
    # The second array submit raises; the first array's jobs must be cancelled.
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
    # The build job records `basic` as a compile failure and its sim job's recompile is
    # killed, writing no envelope; the head maps that to CompileFail, not DispatchFail.
    _mark_stub_builder_verilator(minimal_project)

    class _CompileFailBuild(_FakeBackend):
        def __init__(self):
            super().__init__(write_results=False)  # sim envelope never appears

        def submit_build(self, spec, *, dependency=None):
            write_build_result_json(spec.result_json, built=[], failed=["basic"])
            return super().submit_build(spec, dependency=dependency)

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
    """The row names the build job, its exit status and its error."""
    _mark_stub_builder_verilator(minimal_project)

    class _CompileFailBuild(_FakeBackend):
        def submit_build(self, spec, *, dependency=None):
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
            return super().submit_build(spec, dependency=dependency)

        def submit(self, spec, *, dependency=None, delay_sec=0.0):
            # The gated job reports the build's verdict as a CompileFail with the
            # generic desc; the head enriches it.
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
    # The rewrite is durable: `rb graph results` re-reads the envelope.
    envelope = json.loads(Path(gated["basic"].result_json).read_text())
    assert envelope["result"]["results"]["desc"] == desc


def test_a_sim_failure_is_not_relabelled_as_the_build_job_s_compile_error(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A `failed` entry from the build job does not rewrite a row whose own sim job
    recompiled and then failed in simulation.
    """
    _mark_stub_builder_verilator(minimal_project)

    class _SimFailAfterBuildFail(_FakeBackend):
        def submit_build(self, spec, *, dependency=None):
            write_build_result_json(spec.result_json, built=[], failed=["basic"])
            return super().submit_build(spec, dependency=dependency)

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
    """The head's rewrite requires the same compiler evidence as the sim's gate: a
    `failed` entry without a returncode is a setup error and is not attributed to the
    build job.
    """
    _mark_stub_builder_verilator(minimal_project)

    class _EvidencelessBuildFail(_FakeBackend):
        def submit_build(self, spec, *, dependency=None):
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
            return super().submit_build(spec, dependency=dependency)

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
    """A sha-bearing record with a generic desc means the sim retried after input drift,
    so the head does not rewrite it.
    """
    _mark_stub_builder_verilator(minimal_project)

    class _DriftedBuildFail(_FakeBackend):
        def submit_build(self, spec, *, dependency=None):
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
            return super().submit_build(spec, dependency=dependency)

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
    # Every test is filtered out by the level window: no compile, no jobs, no build job.
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
    # One plan manifest is shared by the build job and every sim job.
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert build.plan_path is not None and Path(build.plan_path).is_file()
    sim = fake_backend.submitted[0]
    assert sim.plan_path == build.plan_path
    # A suite whose command root is not shared keeps the flat .dispatch layout.
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


class _RecordingBackend(_FakeBackend):
    """FakeBackend that also records array submissions and wait calls."""

    def __init__(self, telemetry=None, build_result=None, **kwargs):
        super().__init__(**kwargs)
        self.array_calls = []
        self.wait_calls = 0
        self.telemetry = telemetry or {}
        # What the build job wrote: {built, failed, builds}. The base fake never runs a
        # build job.
        self.build_result = build_result

    def submit_build(self, spec, *, dependency=None):
        handle = super().submit_build(spec, dependency=dependency)
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

    def submit_build(self, spec, *, dependency=None):
        self.build_submitted.append(spec)
        return JobHandle(job_id=f"delayed-build-{len(self.build_submitted)}", spec=spec)

    def submit(self, spec, *, dependency=None, delay_sec=0.0):
        self.submitted.append(spec)
        self.dependencies.append(dependency)
        return JobHandle(job_id=f"delayed-sim-{len(self.submitted)}", spec=spec)

    def wait_all(self, handles, *, extra_wait=0.0):
        # The first plan read happens after both suites have submitted, as with a
        # scheduler that leaves the first suite queued.
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
    # Two configs share one command root; all jobs are in flight before the single wait,
    # without sharing a suite-scoped path.
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
    """Every suite is planned before any submits, and each submits from its own
    environment snapshot.

    The second suite's sweep hook exports `SBATCH_NTASKS=64` in this process; the first
    suite's build and array must not inherit it. After the last submission the process
    keeps the hook's environment.
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

    def submit_build_recording_env(spec, **kwargs):
        seen["build"][Path(spec.test_config_path).name] = os.environ["SBATCH_NTASKS"]
        return real_submit_build(spec, **kwargs)

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
    # Both fixture tests have identical resources: one submit_array call, throttled by
    # max-jobs-per-array.
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
    """The environment can change between a suite's submit and its analysis.

    A later suite's sweep hook runs in this process and can set `SBATCH_*`, so analysis
    must use the environment at submit time. `wait_all` runs after the last submit and
    before the first collect, so mutating the environment there reproduces the hook's
    effect.
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
    # Both halves of the advice describe one submission, made under the environment at
    # submit time.
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
    """The build job's sacct row travels with its artifact too."""
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
    """The compile a test never ran itself appears on its row, folded into the
    envelope's nested ``result.results`` where `rb graph results` reads it.
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
    # The build job's observed builder wins over the head's pre-submit resolution,
    # because a preproc hook can move it.
    assert compile_block["builder"] == "hook-chosen-builder"
    assert compile_block["reused"] is False
    # `group` is build-job bookkeeping, not part of the per-run record.
    assert "group" not in compile_block


def test_an_old_build_envelope_without_compile_records_still_collects(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """Mixed-version fleets degrade, never fail: a build job that writes no ``builds``
    key leaves the sim rows without a compile block.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _build_telemetry_backend(monkeypatch, builds=None)
    backend.build_result["builds"] = None
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
    """Build telemetry is collected in the first pass only; a retry pass does not
    re-query the build job.
    """
    # Share-build capable, so the suite gets a build job to leave out of the second
    # query.
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


def test_randtest_dispatch_fixed_seed_reports_shared_identity_on_every_row(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    recording_backend: _RecordingBackend,
):
    suite_path = minimal_project / "tests.yaml"
    suite_path.write_text(
        suite_path.read_text().replace(
            "    sim_timeout:\n",
            "    sim_timeout:\n"
            "    sim-rand-seed: 41\n"
            "    sim-rand-seed-plusarg: stimulus_seed\n",
            1,
        )
    )

    result, _ = _invoke(["--machine", "randtest", "basic", "2", "--dispatch", "slurm"])

    assert result.exit_code == 0, result.output
    assert [spec.resolved_seed for spec in recording_backend.submitted] == [41, 41]
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    rows = json.loads(payload_line)["payload"]["results"]
    assert [
        (row["seed"]["resolved_seed"], row["seed"]["identity"]) for row in rows
    ] == [
        (41, "tests.yaml::basic::single"),
        (41, "tests.yaml::basic::single"),
    ]


def test_randtest_replay_stays_local(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    recording_backend: _RecordingBackend,
):
    stub_build_runner.canned = TestPassResults(name="basic/results")
    result, _ = _invoke(["randtest", "basic", "3", "-r", "2", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    assert recording_backend.submitted == []


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
    # Under a .dispatch sibling and tagged with the head pid so overlapping runs do not
    # share a manifest.
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
    # A missing envelope for suite 1 must log under suite 1's root; collect re-enters
    # the per-suite command context.
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


def _add_dispatch_resources(project: Path, block: str):
    root_cfg = project / "root_config.yaml"
    root_cfg.write_text(root_cfg.read_text() + block)


def test_no_build_job_when_no_test_can_share_a_build(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A builder that compiles in-job gets no build job, and its elements run unblocked."""
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
    """The one allocation is sized max(sim, compile) field by field.

    ``parallel: 4`` does not reach it: a sim job that compiles for itself runs one
    build, so the reservation and the ``compile_floor`` rows are unscaled.
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
    """The compile block does not inflate sim jobs that only simulate."""
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
    build = fake_backend.build_submitted[0].resources
    assert (build.cpus, build.mem, build.time) == (8, "16G", "02:00:00")
    # Nothing asked for concurrency, so the reservation is unscaled.
    assert fake_backend.build_submitted[0].parallel == 1


def _add_suite_compile(project: Path, block: str):
    """Prepend a suite-level ``compile:`` block to the fixture's tests.yaml, after the
    filetype line.
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
    """A suite's own ``compile:`` beats cfg-dispatch field by field; fields it does not
    state come from cfg-dispatch.
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
    # Sim jobs only simulate: the compile block, at either level, does not affect their
    # reservation.
    sim = fake_backend.submitted[0].resources
    assert (sim.cpus, sim.mem, sim.time) == (1, "2G", "00:20:00")


def test_suite_compile_block_scales_with_compile_parallel(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A suite that overrides only cpus still inherits the root `parallel`."""
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(2))
    _add_suite_compile(minimal_project, "compile:\n  cpus: 6\n")
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    # The suite states no concurrency, so the cluster-wide 2 binds, capped at the 2
    # planned configs.
    assert build.parallel == 2
    assert build.resources.cpus == 12  # 2 x the suite's 6, not 2 x 4
    assert build.resources.mem == "16G"  # cfg-dispatch's, unscaled


def test_suite_compile_parallel_overrides_the_cluster_wide_value(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A suite's `parallel` sizes its build job's cpu reservation, not the root value."""
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(2))
    _add_suite_compile(
        minimal_project,
        'compile:\n  cpus: 8\n  mem: 20G\n  time: "00:30:00"\n  parallel: 1\n',
    )
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert build.parallel == 1  # the suite's, not the cluster-wide 2
    # Nothing was capped here, so the configured value is what it was given.
    assert build.parallel_configured == 1
    assert build.resources.cpus == 8  # the suite's cpus, NOT 16
    assert (build.resources.mem, build.resources.time) == ("20G", "00:30:00")


def test_suite_compile_parallel_raises_above_the_cluster_wide_value(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """The suite's `parallel` wins over the root in both directions; the planned-config
    cap still applies on top.
    """
    _mark_stub_builder_verilator(minimal_project)
    _add_third_test(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(1))
    _add_suite_compile(minimal_project, "compile:\n  cpus: 2\n  parallel: 3\n")
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert build.parallel == 3  # 3 planned configs, so the cap does not bite
    assert build.resources.cpus == 6  # 3 x the suite's 2


def test_suite_compile_parallel_is_still_capped_by_the_planned_configs(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """Two planned configs cannot keep four suite-requested slots busy."""
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(1))
    _add_suite_compile(minimal_project, "compile:\n  cpus: 2\n  parallel: 4\n")
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert build.parallel == 2  # basic + extra, not the suite's 4
    assert build.resources.cpus == 4  # 2 x 2
    # The pre-cap value rides along so the console line quotes the 4 in the suite's
    # tests.yaml.
    assert build.parallel_configured == 4


def test_suite_compile_parallel_never_reaches_an_in_job_compile(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A sim job that compiles for itself runs one build, whatever the suite's
    `parallel` says.
    """
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "00:20:00"\n'
        '  compile:\n    cpus: 2\n    mem: 4G\n    time: "00:10:00"\n',
    )
    _add_suite_compile(minimal_project, "compile:\n  cpus: 3\n  parallel: 4\n")
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    assert fake_backend.build_submitted == []
    assert fake_backend.submitted[0].resources.cpus == 3  # not 12


def test_suite_compile_block_reaches_an_in_job_compile_reservation(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A builder that compiles in its own sim job gets the suite block too."""
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


def _two_geometry_suite(project: Path, *, small: str = "", big: str = ""):
    """Rewrite the fixture as a two-geometry suite.

    Two testbenches over the same sources, each optionally carrying its own ``compile:``
    block, plus a reglvl-5 test on the second one. ``small`` and ``big`` are those
    blocks' YAML, indented for a testbench entry.
    """
    tests_yaml = project / "tests.yaml"
    body = tests_yaml.read_text()
    entry = "  - name: {name}\n    toplevel: tb_basic\n    filelist:\n      - src/example.sv\n"
    body = body.replace(
        entry.format(name="tb_basic") + "tests:\n",
        entry.format(name="tb_basic")
        + small
        + entry.format(name="tb_big")
        + big
        + "tests:\n",
        1,
    )
    assert "tb_big" in body
    body += (
        "  - name: big\n"
        "    desc: the product geometry\n"
        "    model: example\n"
        "    model_path: models.yaml\n"
        "    reglvl: 5\n"
        "    testbench: tb_big\n"
    )
    tests_yaml.write_text(body)


def test_build_job_reserves_the_max_over_the_planned_testbenches(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """One build job gets one allocation, sized per field by its largest build."""
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "00:20:00"\n'
        '  compile:\n    cpus: 4\n    mem: 16G\n    time: "02:00:00"\n',
    )
    _two_geometry_suite(
        minimal_project,
        small="    compile:\n      cpus: 6\n      mem: 6G\n",
        big='    compile:\n      mem: 96G\n      time: "06:00:00"\n',
    )
    _add_suite_compile(minimal_project, 'compile:\n  mem: 8G\n  time: "00:30:00"\n')
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0].resources
    assert build.cpus == 6  # tb_basic's, the larger of the two
    assert build.mem == "96G"  # tb_big's, over the suite's 8G
    assert build.time == "06:00:00"  # tb_big's, over the suite's 00:30:00


def test_an_unselected_testbench_does_not_inflate_the_build_reservation(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A build nobody selected does not fence off memory.

    At the default regression level only the small geometry's test is selected, so the
    product geometry's 96G is not in the maximum.
    """
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "00:20:00"\n'
        '  compile:\n    cpus: 4\n    mem: 16G\n    time: "02:00:00"\n',
    )
    _two_geometry_suite(
        minimal_project,
        big='    compile:\n      mem: 96G\n      time: "06:00:00"\n',
    )
    _add_suite_compile(minimal_project, 'compile:\n  mem: 8G\n  time: "00:30:00"\n')
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0].resources
    assert (build.cpus, build.mem, build.time) == (4, "8G", "00:30:00")


def test_the_testbench_max_is_still_scaled_by_compile_parallel(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """cpus x parallel applies after the maximum, not instead of it."""
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(2))
    _two_geometry_suite(
        minimal_project, big="    compile:\n      cpus: 6\n      mem: 96G\n"
    )
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert build.parallel == 2
    assert build.resources.cpus == 12  # 2 x max(4 root, 6 tb_big)
    # tb_big's 96G plus the 16G cfg-dispatch implies for the unannotated build beside
    # it, unscaled.
    assert mem_to_bytes(build.resources.mem) == 112 * 2**30


def test_an_in_job_compile_uses_its_own_testbenchs_block(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """Each sim job that compiles for itself is sized for its own testbench, not for the
    build job's aggregate.
    """
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "00:20:00"\n'
        '  compile:\n    cpus: 2\n    mem: 4G\n    time: "00:10:00"\n',
    )
    _two_geometry_suite(minimal_project, big="    compile:\n      mem: 96G\n")
    _add_suite_compile(minimal_project, "compile:\n  mem: 8G\n")
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    assert fake_backend.build_submitted == []
    by_test = {spec.test_name: spec.resources for spec in fake_backend.submitted}
    # The two tests on the small geometry keep the suite's 8G...
    assert by_test["basic"].mem == "8G"
    assert by_test["extra"].mem == "8G"
    assert by_test["big"].mem == "96G"


def test_build_job_sums_the_memory_of_the_builds_that_overlap(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """`compile.parallel: 2` reserves both elaborations' peaks at once, not the maximum
    of the two.
    """
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(2))
    _two_geometry_suite(
        minimal_project,
        small='    compile:\n      mem: 20G\n      time: "00:30:00"\n',
        big='    compile:\n      mem: 96G\n      time: "01:30:00"\n',
    )
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert build.parallel == 2
    # 96G + 20G, the two builds that can be in flight together.
    assert mem_to_bytes(build.resources.mem) == 116 * 2**30
    # The wall clock stays at cfg-dispatch's 2h; nothing the testbenches state exceeds
    # it.
    assert time_to_seconds(build.resources.time) == 7200


def test_build_job_time_is_the_serial_total_at_parallel_one(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """One worker compiles both benches back to back, so their times add."""
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "00:20:00"\n'
        '  compile:\n    cpus: 4\n    mem: 16G\n    time: "00:10:00"\n',
    )
    _two_geometry_suite(
        minimal_project,
        small='    compile:\n      time: "00:30:00"\n',
        big='    compile:\n      time: "01:00:00"\n',
    )
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert build.parallel == 1
    # 30m + 60m, not max(30m, 60m): a maximum times the job out mid-queue.
    assert time_to_seconds(build.resources.time) == 5400
    # mem said nothing at testbench level, so the whole-job value stands.
    assert build.resources.mem == "16G"


def test_every_distinct_planned_build_reaches_the_aggregation(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """One reservation per distinct build, not per distinct testbench.

    Tests on one testbench that differ in plusdefines, builder or model compile
    separately and each hold their own peak. Configs whose head-visible ingredients are
    identical collapse into one build.
    """
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(2))
    (minimal_project / "sweep.py").write_text(
        "import copy\n"
        "from rtl_buddy.config.dispatch import TestbenchCompileFile\n"
        "out_test_cfgs = []\n"
        # a: own block. b: bigger block. c: identical to b in every head-visible
        # ingredient, so the same build. d: b's block plus its own plusdefine, a
        # separate verilation.
        "for suffix, mem, pd in (\n"
        "    ('a', '20G', None),\n"
        "    ('b', '96G', None),\n"
        "    ('c', '96G', None),\n"
        "    ('d', '96G', {'WIDTH': 64}),\n"
        "):\n"
        "    cfg = copy.deepcopy(test_cfg)\n"
        "    cfg.name = test_cfg.name + '.' + suffix\n"
        "    cfg.tb.compile = TestbenchCompileFile(mem=mem)\n"
        "    if pd is not None:\n"
        "        cfg.pd = dict(pd)\n"
        "    out_test_cfgs.append(cfg)\n"
    )
    suite_path = minimal_project / "tests.yaml"
    suite_path.write_text(
        suite_path.read_text().replace(
            "    sweep:\n", "    sweep:\n      path: sweep.py\n", 1
        )
    )
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert build.parallel == 2
    # Three distinct builds (b and c are one): the two largest that can overlap are the
    # two 96G verilations, not 96G + 20G.
    assert mem_to_bytes(build.resources.mem) == 192 * 2**30


def test_identical_planned_configs_are_one_reservation(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """Tests that share a testbench, model, builder and plusdefines are one build and
    one reservation.
    """
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(2))
    _two_geometry_suite(minimal_project, small="    compile:\n      mem: 20G\n")
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    # `basic` and `extra` are one compile on tb_basic, so one 20G build is in the sums.
    # `big` states no block and contributes cfg-dispatch's 16G.
    build = fake_backend.build_submitted[0]
    assert mem_to_bytes(build.resources.mem) == 36 * 2**30


def _two_tests_on_one_30g_testbench(project: Path, extra: str = ""):
    """One 30G testbench, two tests identical but for ``extra`` YAML."""
    tests_yaml = project / "tests.yaml"
    body = tests_yaml.read_text().replace(
        "    filelist:\n      - src/example.sv\n",
        "    filelist:\n      - src/example.sv\n    compile:\n      mem: 30G\n",
        1,
    )
    body += (
        "  - name: twin\n"
        "    desc: the same compile again\n"
        "    model: example\n"
        "    model_path: models.yaml\n"
        "    reglvl: 0\n"
        "    testbench: tb_basic\n" + extra
    )
    tests_yaml.write_text(body)


def test_a_preproc_hook_makes_a_test_its_own_build(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A test with a preprocessing hook counts as its own compile, because the hook may
    set plusdefines after the key is snapshotted.
    """
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(2))
    (minimal_project / "pre.py").write_text("pass\n")
    _two_tests_on_one_30g_testbench(
        minimal_project, extra="    preproc:\n      path: pre.py\n"
    )
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text().replace(
            "    preproc:\n    postproc:\n",
            "    preproc:\n      path: pre.py\n    postproc:\n",
            1,
        )
    )
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert build.parallel == 2
    # Two builds of the same 30G testbench, both peaks held at once.
    assert mem_to_bytes(build.resources.mem) == 60 * 2**30


def _two_preproc_tests_on_one_30g_testbench(
    project: Path, *, basic_flag="", twin_flag="", suite_flag=""
):
    """`basic` and `twin` on one 30G testbench, both with a preproc hook; ``*_flag`` is extra YAML for that layer."""
    (project / "pre.py").write_text("pass\n")
    _two_tests_on_one_30g_testbench(
        project, extra="    preproc:\n      path: pre.py\n" + twin_flag
    )
    tests_yaml = project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text()
        .replace(
            "    preproc:\n    postproc:\n",
            "    preproc:\n      path: pre.py\n" + basic_flag + "    postproc:\n",
            1,
        )
        .replace("testbenches:\n", suite_flag + "testbenches:\n", 1)
    )


@pytest.mark.parametrize(
    "layers",
    [
        pytest.param(
            {
                "basic_flag": "    preproc-sets-plusdefines: false\n",
                "twin_flag": "    preproc-sets-plusdefines: false\n",
            },
            id="per-test",
        ),
        pytest.param(
            {"suite_flag": "preproc-sets-plusdefines: false\n"}, id="per-suite"
        ),
    ],
)
def test_a_hook_declared_not_to_set_plusdefines_shares_its_build(
    minimal_project: Path,
    fake_backend: _FakeBackend,
    layers,
):
    """`preproc-sets-plusdefines: false` keys a hooked test like a hook-less one, so identical tests reserve one build."""
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(2))
    _two_preproc_tests_on_one_30g_testbench(minimal_project, **layers)
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert mem_to_bytes(build.resources.mem) == 30 * 2**30


def test_a_test_flag_overrides_the_suite_preproc_declaration(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A test that says its hook does set plusdefines stays its own build under a suite-wide `false`."""
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(2))
    _two_preproc_tests_on_one_30g_testbench(
        minimal_project,
        suite_flag="preproc-sets-plusdefines: false\n",
        twin_flag="    preproc-sets-plusdefines: true\n",
    )
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert mem_to_bytes(build.resources.mem) == 60 * 2**30


def test_without_a_preproc_hook_identical_tests_are_one_build(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """Without a hook nothing can mutate the key, so identical tests are one compile."""
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(2))
    _two_tests_on_one_30g_testbench(minimal_project)
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert mem_to_bytes(build.resources.mem) == 30 * 2**30


def test_assertion_mode_splits_a_build(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """`assertions: true` changes the compile command, so it splits the compile key."""
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(2))
    tests_yaml = minimal_project / "tests.yaml"
    body = tests_yaml.read_text()
    body = body.replace(
        "    filelist:\n      - src/example.sv\n",
        "    filelist:\n      - src/example.sv\n    compile:\n      mem: 30G\n",
        1,
    )
    # `basic` keeps the default (false); this one differs in nothing else.
    body += (
        "  - name: asserted\n"
        "    desc: same compile but with SVA in\n"
        "    model: example\n"
        "    model_path: models.yaml\n"
        "    reglvl: 0\n"
        "    assertions: true\n"
        "    testbench: tb_basic\n"
    )
    tests_yaml.write_text(body)

    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert build.parallel == 2
    # Both peaks are held at once; a key blind to `assertions` would reserve one 30G
    # build.
    assert mem_to_bytes(build.resources.mem) == 60 * 2**30


def test_self_compiling_configs_each_count_as_their_own_build(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A builder that cannot share compiles per test, so each test is a build.

    The key carries the test name for those entries, because their `group_dir` is the
    per-test simv.
    """
    # A second builder that can share, so the suite submits a build job at all; the
    # fixture's `echo` family cannot.
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text().replace(
            "\ncfg-verible:",
            '  - name: "stub-vrl"\n'
            '    builder: "echo"\n'
            '    simulator-family: "verilator"\n'
            '    builder-simv: "obj_dir/simv"\n'
            "    sim-rand-seed: 1\n"
            '    sim-rand-seed-prefix: "+seed="\n'
            "    builder-opts:\n"
            "      debug:\n"
            '        compile-time: "--no-op"\n'
            '        run-time: "--no-op"\n'
            "\ncfg-verible:",
            1,
        )
    )
    _add_dispatch_resources(minimal_project, _compile_parallel_config(2))
    tests_yaml = minimal_project / "tests.yaml"
    body = tests_yaml.read_text()
    entry = "  - name: {name}\n    toplevel: tb_basic\n    filelist:\n      - src/example.sv\n"
    body = body.replace(
        entry.format(name="tb_basic") + "tests:\n",
        entry.format(name="tb_basic")
        + "    compile:\n      mem: 30G\n"
        + entry.format(name="tb_shared")
        + "    compile:\n      mem: 10G\n"
        + "tests:\n",
        1,
    )
    # `basic` and `extra` share everything the head sees and still compile separately.
    body += (
        "  - name: shared\n"
        "    desc: the one config whose builder can share a build\n"
        "    model: example\n"
        "    model_path: models.yaml\n"
        "    reglvl: 0\n"
        "    builder: stub-vrl\n"
        "    testbench: tb_shared\n"
    )
    tests_yaml.write_text(body)

    # -l 5 plans both self-compiling tests.
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert build.parallel == 2
    # Three builds: basic(30G), extra(30G) and shared(10G). The overlapping pair is the
    # two 30G self-compiles, not 30 + 10.
    assert mem_to_bytes(build.resources.mem) == 60 * 2**30


def _add_third_test(project: Path):
    """A third planned config, so ``parallel`` can be the binding limit."""
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
    """N concurrent builds get N times the cpus, and only cpus; mem and time are the
    project's to size.
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
    """Two planned configs cannot keep three build slots busy, so the cpus scaling is
    capped at the planned configs.
    """
    _mark_stub_builder_verilator(minimal_project)
    _add_dispatch_resources(minimal_project, _compile_parallel_config(3))
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    build = fake_backend.build_submitted[0]
    assert build.parallel == 2  # basic + extra, not the configured 3
    assert build.parallel_configured == 3  # what cfg-dispatch actually says
    assert build.resources.cpus == 8  # 2 x 4, not 3 x 4


def test_single_planned_config_leaves_the_build_reservation_alone(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """One config is one build, and its spec is unscaled."""
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
    """A dispatched randtest on a builder with no shared-build support compiles once in
    the build job; every element waits for it and short-circuits on its stamp.
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
    """Distinct tests each own their `artefacts/<test>/`, so no build job serializes
    them.
    """
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
    """A mixed-builder suite gates the self-compiling test on the build job too.

    The build job runs PRE and COMPILE for the whole plan, so it writes into a
    self-compiling test's artefact dir; an ungated element would be a second writer. One
    shareable test puts a build job in the plan, and the unshareable one beside it waits
    for it.
    """
    _mark_stub_builder_verilator(minimal_project)
    _add_second_builder(minimal_project, name="unshareable", family="questa")
    tests_yaml = minimal_project / "tests.yaml"
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


def _raise_and_reduce_backend(minimal_project, monkeypatch):
    """`basic` is over-reserved on time and killed on memory; `extra` fits both.

    The analyzer emits time before mem per test, so the one `raise` is second of four
    and every other finding is a `reduce`.
    """
    backend = _RecordingBackend(
        telemetry={
            "fake-1": {
                "state": "OUT_OF_MEMORY",
                "elapsed_s": 10,
                "timelimit_s": 3600,
                "req_mem_bytes": 8 * 2**30,
                "max_rss_bytes": 8 * 2**30,
            },
            "fake-2": {
                "state": "COMPLETED",
                "elapsed_s": 10,
                "timelimit_s": 3600,
                "req_mem_bytes": 8 * 2**30,
                "max_rss_bytes": 2**30,
            },
        }
    )
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    _mark_stub_builder_verilator(minimal_project)
    return ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]


def test_reservation_advice_payload_lists_raise_findings_first(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The one reservation that failed leads the list; reduces keep their order."""
    argv = _raise_and_reduce_backend(minimal_project, monkeypatch)
    result, _ = _invoke(["--machine", *argv])
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    assert [(a["test"], a["resource"], a["direction"]) for a in advice] == [
        ("basic", "mem", "raise"),
        ("basic", "time", "reduce"),
        ("extra", "time", "reduce"),
        ("extra", "mem", "reduce"),
    ]


def test_reservation_advice_table_lists_raise_findings_first(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The human table is rendered from the same ordered list."""
    argv = _raise_and_reduce_backend(minimal_project, monkeypatch)
    result, _ = _invoke(argv)
    assert result.exit_code == 0, result.output
    advice_table = result.output[result.output.index("Reservation Advice") :]
    directions = re.findall(r"\b(raise|reduce) ", advice_table)
    assert directions == ["raise", "reduce", "reduce", "reduce"]


def _raise_in_the_second_suite_backend(minimal_project, monkeypatch):
    """Two colocated suites; only the second one's first test is OOM-killed.

    Suites are analyzed in submission order, so every `reduce` is found before the run's
    single `raise`.
    """
    fits = {
        "state": "COMPLETED",
        "elapsed_s": 10,
        "timelimit_s": 3600,
        "req_mem_bytes": 8 * 2**30,
        "max_rss_bytes": 2**30,
    }
    killed = dict(fits, state="OUT_OF_MEMORY", max_rss_bytes=8 * 2**30)
    backend = _RecordingBackend(
        telemetry={
            "fake-1": fits,
            "fake-2": fits,
            "fake-3": killed,
            "fake-4": fits,
        }
    )
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    _write_colocated_suites(minimal_project)
    _mark_stub_builder_verilator(minimal_project)
    return ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]


def test_rightsize_advice_events_are_raise_first_across_suites(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The logged events follow the regression-wide order, not each suite's order."""
    argv = _raise_in_the_second_suite_backend(minimal_project, monkeypatch)
    result, _ = _invoke(["--machine", *argv])
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    assert advice[0]["direction"] == "raise"
    assert advice[0]["test"] == "other_basic"
    assert advice[0]["resource"] == "mem"
    assert [a["direction"] for a in advice[1:]] == ["reduce"] * (len(advice) - 1)

    log_lines = (minimal_project / "rtl_buddy.log").read_text().splitlines()
    records = [json.loads(line) for line in log_lines if line.strip()]
    logged = [r for r in records if r.get("event") == "rightsize.advice"]
    assert [(r["test"], r["resource"], r["direction"]) for r in logged] == [
        (a["test"], a["resource"], a["direction"]) for a in advice
    ]


def test_whole_core_rounding_produces_no_cpus_advice_end_to_end(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The head's own `--cpus-per-task` reaches right-sizing: efficiency is judged
    against the 1 cpu the head submitted, not the 2 the site allocated, so nothing is
    advised.
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

    `cfg-dispatch.sbatch-args` states a cpu request of 4 while the project resolves 1.
    The head records no request, so `ReqCPUS` carries the 4. A task count instead of a
    cpu count is covered separately.
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
    # `-D` so the DEBUG line reaches the console: rb reconfigures the root logger, so
    # caplog never sees a CLI run's events.
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
    # Analysed against the 4 the override submitted, not the 1 the YAML resolved to.
    assert cpus["reserved"] == "4"
    assert cpus["allocated"] is None
    assert cpus["suggested"] == "2"  # ceil(4 x 0.25 x 1.5)
    # The hint names the argument, not the field it masks.
    assert cpus["edit_hint"]["path"] == "cfg-dispatch.sbatch-args"
    assert (
        f"sbatch-args `{named}` sets this job's cpu request, "
        "superseding tests[name=basic].resources.cpus" in cpus["edit_hint"]["note"]
    )
    # time still names its own field; a cpu argument supersedes nothing there.
    (time_row,) = [a for a in advice if a["resource"] == "time"]
    assert time_row["edit_hint"]["path"] == "tests[name=basic].resources.time"
    # The run says why the advice came from sacct rather than from the reservation.
    assert "sbatch-args" in result.output
    assert named in result.output
    assert "ReqCPUS" in result.output


def test_an_sbatch_env_var_reaches_the_analysis_end_to_end(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """`SBATCH_NTASKS` in the environment is read by sbatch, so it is recognised as the
    cpu request.

    The environment is not sanitized: the analysis falls back to `ReqCPUS` and the hint
    names the variable rather than a YAML field.
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
    # Analysed against the 4 the environment asked for, not the 1 the YAML resolved to.
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
    """The generated `--cpus-per-task` beats `SBATCH_CPUS_PER_TASK`, so nothing changes.

    The command line takes precedence over the environment in sbatch, so the head keeps
    the request it knows.
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
                "req_cpus": 2,
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
        # Node selection restricts nodes and hardware threads; the generated
        # `--cpus-per-task` still states the request.
        "--threads-per-core=2",
        "-B 2:4:1",
        # Placement maxima cap where `--ntasks` tasks may land; alone they request
        # nothing.
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
    """These options shape placement and do not set the cpu request.

    The generated `--cpus-per-task=1` still states the request, so the head keeps using
    it.
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
                "req_cpus": 2,
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
    (time_row,) = [a for a in advice if a["resource"] == "time"]
    assert time_row["edit_hint"]["path"] == "tests[name=basic].resources.time"


def test_a_lone_ntasks_override_does_not_claim_to_take_the_suggestion(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """`--ntasks` is a task count, so the note does not tell the reader to write the cpu
    figure into it; the finding still carries the whole-job figure.
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
    """`--ntasks` x `--cpus-per-task` is a product, so neither argument takes the
    number; the advice still reaches the reader.
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
    """Advice for a job that compiled respects the compile floor.

    The allocation is max(sim, compile). A suggestion under that floor is clamped up to
    it and re-attributed to the field that governs; one clamped back to the current
    reservation is dropped.
    """
    _telemetry_backend(monkeypatch)  # elapsed 10s of 1h, 1G of 8G reserved
    # No simulator-family override: the inferred `echo` family cannot share a build, so
    # the sim job compiles for itself.
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

    # time: 10s of 1h would suggest the 5-minute floor, but the compile needs 30
    # minutes, so cfg-dispatch.compile.time is the field to move.
    (time_a,) = [a for a in advice if a["resource"] == "time"]
    assert time_a["phase"] == "compile+sim"
    assert time_a["direction"] == "reduce"
    assert time_a["suggested"] == "00:30:00"
    assert time_a["edit_hint"]["path"] == "cfg-dispatch.compile.time"
    assert time_a["edit_hint"]["file"].endswith("root_config.yaml")

    # mem: the reserved 8G is the compile reservation, so every reduce clamps back to it
    # and is dropped.
    assert [a for a in advice if a["resource"] == "mem"] == []


def test_machine_payload_carries_build_job_reservation_advice(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The build job's own reservation gets a `compile` advice row.

    It has no suite_results row, so per-test analysis never sees it.
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
            # 8 cpus (4 per build x parallel 2) used 60 of 480 core-seconds: badly
            # over-reserved.
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
    # -l 5 plans both fixture tests, so the cap does not collapse `parallel: 2` to 1.
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

    # No cpus row: with two build slots, efficiency cannot separate idle slots from
    # under-used compilers (see tests/test_dispatch_rightsize.py).
    assert "cpus" not in build_advice

    # time is still advised: wall clock is not inflated by concurrent builds.
    time_a = build_advice["time"]
    assert time_a["test"] == "(build job)"
    assert time_a["reserved"] == "02:00:00"
    assert time_a["direction"] == "reduce"
    assert time_a["edit_hint"]["path"] == "cfg-dispatch.compile.time"
    assert time_a["edit_hint"]["file"].endswith("root_config.yaml")
    assert "note" not in time_a["edit_hint"]


def test_build_advice_uses_the_suite_resolved_parallel_for_its_cpus_gate(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The `parallel > 1` gate reads the resolved value, not the root.

    The suite sets `parallel: 1`, so the job ran one build at a time, its whole-job
    efficiency is its per-build one, and the cpus row is offered.
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
            # 4 cpus, NOT 8: the suite's `parallel: 1` is what sized this.
            "alloc_cpus": 4,
            "total_cpu_s": 30,
        },
    )
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "01:00:00"\n'
        '  compile:\n    cpus: 4\n    mem: 8G\n    time: "02:00:00"\n'
        "    parallel: 2\n",
    )
    _add_suite_compile(minimal_project, "compile:\n  parallel: 1\n")
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

    cpus_a = build_advice["cpus"]
    assert cpus_a["test"] == "(build job)"
    assert cpus_a["reserved"] == "4"  # the unscaled per-build value
    assert cpus_a["direction"] == "reduce"
    # One slot, so there is no product to decompose and no lever to name.
    assert "the build job reserved 4" in cpus_a["edit_hint"]["note"]
    assert "compile.parallel" not in cpus_a["edit_hint"]["note"]
    assert cpus_a["edit_hint"]["path"] == "cfg-dispatch.compile.cpus"


def test_build_advice_names_the_suite_file_for_a_field_the_suite_overrode(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """Advice names the file that holds the winning value.

    A suite-level `compile.time` beats cfg-dispatch, so the advice points at the suite's
    tests.yaml, not at `cfg-dispatch.compile.time`.
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
    """Re-dispatching an unchanged suite gives no build time advice.

    Every build short-circuits on its stamp, so the build job is short with near-zero
    cpu time. The envelope's ``reused`` flags keep sacct alone from advising a time
    reduction that the next real RTL change would time out on.
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
    """A backend that reports no usage gets no build advice.

    `local-parallel`'s `collect_telemetry` returns ``{}``, and no verdict is invented
    from nothing.
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
    # A row whose builder name does not resolve must not abort the regression during
    # advice analysis.
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

    # Every builder lookup during analysis fails, as if the name were missing from
    # cfg-rtl-builder.
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
    """``-j`` reaches the backend as its pool size."""
    backend, seen = _FakeBackend(), {}

    def _capture(name, cfg, *, config_path=None):
        seen["name"], seen["jobs"] = name, cfg.jobs
        seen["config_path"] = config_path
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
    # The config the backend was built from travels with it, so advice about an
    # `sbatch-args` override can name its file.
    assert seen == {
        "name": "local-parallel",
        "jobs": 3,
        "config_path": str(minimal_project / "root_config.yaml"),
    }


def test_jobs_flag_is_rejected_against_a_backend_without_a_pool(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """A concurrency knob the backend cannot honour is rejected, not silently ignored."""
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
    """An unscheduled backend's failures are not explained by `afterok`; the diagnostic
    names what can actually have happened.
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
    """`-r` skips dispatch, so `-j` validation runs before the replay branch; with a
    pool backend the flag is legal but unused, and the ignored-flag warning names it.
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
    """A gated element that still compiles signals that the build's stamp failed and
    every sibling is compiling too.
    """
    result, _ = _invoke(["randtest", "basic", "3", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    assert len(fake_backend.build_submitted) == 1
    assert all(spec.expect_prebuilt for spec in fake_backend.submitted)


def test_ungated_jobs_are_not(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """With no build job nothing is prebuilt and each directory has one writer, so
    compiling is expected.
    """
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
    """The head rule for ``--rebuild`` under dispatch.

    Only the build job, the single writer of the shared directory, gets ``--rebuild``.
    Handing it to the gated array too would defeat the fresh stamp and put every element
    into that directory at once. ``local-parallel`` follows the same rule through the
    same head code, so this is parametrised over both backends.
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
    """With no build job each element owns its build dir, so ``--rebuild`` reaches it."""
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
    """Without ``--rebuild`` no spec carries it."""
    result, _ = _invoke(["randtest", "basic", "3", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    assert not fake_backend.build_submitted[0].rebuild
    assert not any(spec.rebuild for spec in fake_backend.submitted)


def test_a_retry_carries_rebuild_but_never_adds_it(minimal_project: Path):
    """A gated element denied ``--rebuild`` on its first attempt does not gain it on its
    retry; one that holds it (its own per-test dir) keeps it if the attempt died
    before compiling.
    """
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
    """The planner and the runtime agree on what can share a build.

    A VCS builder with an absolute `builder-simv:` declines sharing at runtime, so the
    planner sizes it for its own compile and submits no build job.
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
    assert fake_backend.build_submitted == []


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
    """The job-id line reaches the console before `wait_all` blocks, because after a
    head death the ids are the only post-mortem route.
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
    assert fields["jobs"] == 3 + (1 if fields.get("build_job") else 0)


LICENSE_BANNER = "Queuing for License... (Licensed number of users already reached)\n"


class _RetryBackend(_FakeBackend):
    """A fleet whose jobs die the way a license-queue kill dies.

    Each job writes the sim's capture (with or without the queue banner) and then either
    leaves no envelope, reported as a scheduler ``TIMEOUT``, or from
    ``passes_on_attempt`` onward writes a PASS.
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
        # Reserved-vs-used numbers in every row, so a retry fleet can exercise
        # reservation advice.
        self.cpu_telemetry = cpu_telemetry or {}
        self.banner = banner
        self.passes_on_attempt = passes_on_attempt
        self.state = state
        # `capture` False: the job never ran and wrote no output, like a sim whose build
        # job failed.
        self.capture = capture
        # A real `rb _build-job` always writes its result file; `build_result` False is
        # a build job that died before writing one.
        self.build_result = build_result
        self.attempts: dict[str, int] = {}
        self.delays: list[float] = []
        self.log_paths: list = []
        self.states: dict[str, str] = {}
        self.wait_calls = 0

    def submit_build(self, spec, *, dependency=None):
        # A real build job always writes its result file; the head reads its absence as
        # the gate never opening.
        if self.build_result:
            write_build_result_json(spec.result_json, built=[], failed=[])
        return super().submit_build(spec, dependency=dependency)

    def submit(self, spec, *, dependency=None, delay_sec=0.0):
        self.submitted.append(spec)
        self.dependencies.append(dependency)
        self.delays.append(delay_sec)
        self.log_paths.append(spec.log_path)
        attempt = self.attempts.get(spec.test_name, 0) + 1
        self.attempts[spec.test_name] = attempt
        job_id = f"fake-{len(self.submitted)}"

        # Where the sim's own output lands: per run for a seed fan-out.
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
    """Stand in for `create_dispatch_backend`, keeping the `sbatch_args` and config it
    was built with.

    Right-sizing reads cpu-request overrides off the backend, so a fake that ignores
    `cfg` would hide that wiring.
    """

    def factory(name, cfg, *, config_path=None):
        backend.effective_sbatch_args = list(getattr(cfg, "sbatch_args", None) or [])
        # The file the arguments came from, so an `sbatch-args` edit hint can name it.
        backend.effective_sbatch_args_path = config_path
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
    # A green run that needed two attempts must not read like one that needed none; rb
    # CLI events are only visible in the output.
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
    # One initial submission plus the two the budget allows; delays double, and retries
    # carry no build dependency.
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
    """The first attempt's banner is the evidence for the retry and is kept."""
    _enable_retry(minimal_project, attempts=1)
    backend = _use_backend(monkeypatch, _RetryBackend(passes_on_attempt=None))

    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 1, result.output
    first, retried = backend.log_paths
    assert Path(first).name == "fake-single.log"
    assert Path(retried).name == "fake-single-retry1.log"
    # The envelope path is shared by the job and the head, so it must not move between
    # attempts.
    assert backend.submitted[0].result_json == backend.submitted[1].result_json


def test_without_a_retry_block_a_license_queue_kill_still_fails_once(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Retry is off by default."""
    backend = _use_backend(monkeypatch, _RetryBackend(passes_on_attempt=2))

    result, _ = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 1, result.output
    assert _rows(result)["basic"]["result"] == "FAIL"
    assert len(backend.submitted) == 1
    assert backend.wait_calls == 1
    # No attempt count appears in the row when retry is off.
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
    """A backend shaped like ``local-parallel``: `scheduled` is False and
    ``collect_telemetry`` is empty, so retry must not require scheduler state.
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
    """max-wait is not spent on the backoff hold: the retry round's deadline is widened
    by the delay.
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
    """A sim whose gating build job died without writing its result never launched, so it is not retried.

    A banner left in `artefacts/basic/test.log` by an earlier run must not make the head
    resubmit that sim ungated.
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
    assert backend.dependencies == ["fake-build"]


def test_a_stale_capture_from_an_earlier_run_is_not_evidence(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """`artefacts/<test>/test.log` is not cleaned between runs, so a banner that
    predates this attempt's submission does not justify a retry.
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
    """Banner, then real simulator output, then the reservation ran out: not retried,
    since it is a genuine hang.
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
    """A refused retry degrades to the collected rows instead of aborting with no
    summary or machine payload.
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
    """The degrade-to-collected contract covers scheduler refusals, not head-side bugs;
    a TypeError propagates.
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


def test_single_test_dispatch_submits_one_build_and_one_gated_job(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """`rb test <name> --dispatch` is the regression plan narrowed to one test: one
    build job, one sim job gated on it, and the result collected from the job's
    envelope.
    """
    _mark_stub_builder_verilator(minimal_project)
    result, rb = _invoke(["test", "basic", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output

    assert [spec.test_name for spec in fake_backend.submitted] == ["basic"]
    assert len(fake_backend.build_submitted) == 1
    assert fake_backend.dependencies == ["fake-build"]
    assert fake_backend.waited
    assert not fake_backend.cancelled

    # Dispatch implies share_build; the sim job carries a reservation and a single
    # unnumbered run.
    assert rb.share_build is True
    (spec,) = fake_backend.submitted
    assert spec.share_build is True
    assert spec.run_id is None
    assert spec.resources.time is not None
    assert spec.result_json.is_file()
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
    """`rb test` defaults the builder mode to `debug` and `rb regression` to `reg`;
    dispatch carries the command's default into its jobs.
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
    """The plan is narrowed to the named test, because `rb test` applies no level
    filter.
    """
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
    """With no test name the whole suite is selected, dispatched or not, as in the
    in-process path.
    """
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
    """Without `--dispatch` and `cfg-dispatch`, nothing is submitted and the stubbed
    TestRunner runs in-process.
    """
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
    """`-j` must mean something on `rb test` too, or be rejected."""
    result, _ = _invoke(["test", "basic", "--dispatch", "slurm", "-j", "4"])
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "max-jobs-per-array" in str(result.exception)


def test_jobs_on_test_without_dispatch_is_rejected_not_dropped(
    minimal_project: Path,
):
    """`rb test` never reads `cfg-dispatch.backend`, so a bare `-j` is rejected."""
    result, _ = _invoke(["test", "basic", "-j", "4"])
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "the backend is local" in str(result.exception)


def test_dispatch_flags_are_validated_before_the_list_short_circuit(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """`--list` runs nothing, so a dispatch flag beside it is rejected."""
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
    """A mistyped backend name is not quoted back as valid; the error names the real
    choices.
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
    """Dispatching `rb test` is opt-in per invocation.

    `rb regression` and `rb randtest` default their backend from `cfg-dispatch.backend`;
    `rb test` does not, so a project configured with `backend: slurm` does not queue
    single-test runs.
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
    """`rb test --early-stop` works under a project that configured a cluster backend,
    without advice to drop a `--dispatch` flag.
    """
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(root_cfg.read_text() + "\ncfg-dispatch:\n  backend: slurm\n")
    result, _ = _invoke(["--early-stop", "comp", "test", "basic"])
    assert result.exit_code == 0, result.output
    assert fake_backend.submitted == []


def test_cfg_dispatch_settings_still_configure_an_opted_in_test_run(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """Only `backend` is ignored: once `--dispatch` opts in, the rest of `cfg-dispatch`
    configures the run.
    """
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
    """Commands that read the config name it in the rejection, because "run without
    --dispatch" is unactionable when no flag was passed.
    """
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
    """One test whose builder cannot share a build gets no build job; the lone sim job
    compiles in its own allocation, ungated.
    """
    # The fixture's inferred `echo` family has no shared-build support.
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
    """`-n` and `-l` reach the job as its `--seed-mode`."""
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
    """Ctrl-C on the head cancels the build and sim jobs before it exits and releases
    the tree lock.
    """

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
    """The key is absent, not empty, when nothing was dispatched, as in the regression
    payload.
    """
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

    The first attempt recorded the resolved 1 cpu. A later suite's sweep hook then
    exported `SBATCH_NTASKS=4`, and `_resubmit_retryable` submits into that environment,
    so the retry's row must record the changed request. The first `wait_all` is that
    window.
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
    """Row metadata describes the attempt whose telemetry sits beside it.

    When the retry is refused by `sbatch` or its wait fails, the head keeps the first
    attempt's results and telemetry, so it also keeps the first attempt's reservation.
    The resolved request is 1 cpu, which the `cpus > 1` guard drops, so no cpus row
    appears.
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
    # The first attempt asked for the resolved 1 cpu, and a one-cpu reservation has no
    # cpus advice.
    assert [a for a in advice if a["resource"] == "cpus"] == []


def _use_backend_with_fixed_args(
    monkeypatch, backend, sbatch_args, *, args_config_path=None
):
    """A backend built from a different config than the suite's.

    `_resolve_dispatch_backend` runs once from the orchestration `root_config.yaml`,
    while `root_cfg` is rebuilt per suite. `args_config_path` is the orchestration
    config the backend's arguments came from; `config_path` and `cfg` are dropped
    because this fake stands for a backend built elsewhere.
    """

    def factory(name, cfg, *, config_path=None):
        backend.effective_sbatch_args = list(sbatch_args)
        backend.effective_sbatch_args_path = args_config_path
        return backend if name not in (None, "local") else None

    monkeypatch.setattr(rtl_buddy_module, "create_dispatch_backend", factory)
    return backend


def test_an_override_only_the_backend_carries_is_still_found(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The suite's `cfg-dispatch` is not what `sbatch` received.

    In a multi-root regression the backend is built once from the orchestration config,
    so the suite's `sbatch-args` can be empty while the backend appends `--ntasks=4`.
    The override is read from the backend.
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


def test_the_override_hint_names_the_config_the_backend_was_built_from(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The machine hint's `file` names the config that holds the override, which is the
    backend's, not the suite's.
    """
    orchestration_cfg = "/proj/orchestration/root_config.yaml"
    backend = _use_backend_with_fixed_args(
        monkeypatch,
        _RecordingBackend(
            telemetry={
                "fake-1": {
                    "state": "COMPLETED",
                    "elapsed_s": 100,
                    "timelimit_s": 3600,
                    "req_mem_bytes": 8 * 2**30,
                    "alloc_cpus": 8,
                    "req_cpus": 8,
                    "total_cpu_s": 200.0,  # 0.25 efficiency against those 8
                }
            }
        ),
        ["--cpus-per-task=8"],
        args_config_path=orchestration_cfg,
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
    assert cpus["edit_hint"]["path"] == "cfg-dispatch.sbatch-args"
    assert cpus["edit_hint"]["file"] == orchestration_cfg
    assert str(minimal_project) not in cpus["edit_hint"]["file"]


def test_a_single_root_run_hints_at_its_own_root_config(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """With one root, the hint names that root by absolute path."""
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text()
        + "\ncfg-dispatch:\n"
        + "  sbatch-args: [--cpus-per-task=8]\n"
    )
    backend = _use_backend(
        monkeypatch,
        _RecordingBackend(
            telemetry={
                "fake-1": {
                    "state": "COMPLETED",
                    "elapsed_s": 100,
                    "timelimit_s": 3600,
                    "req_mem_bytes": 8 * 2**30,
                    "alloc_cpus": 8,
                    "req_cpus": 8,
                    "total_cpu_s": 200.0,
                }
            }
        ),
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
    assert cpus["edit_hint"]["path"] == "cfg-dispatch.sbatch-args"
    assert cpus["edit_hint"]["file"] == str(root_cfg)


def test_a_suite_override_the_backend_never_had_makes_no_false_hint(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The suite's config claims an override that sbatch never got, so the hint does not
    name `sbatch-args`.
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


def _stamped_row(test_name, sha, simv, build_dir=None):
    results = TestPassResults(name=f"{test_name}/results")
    results.results["build_stamp"] = {"fingerprint_sha": sha, "simv": simv}
    if build_dir is not None:
        results.results["build_stamp"]["build_dir"] = build_dir
    return {"test_name": test_name, "randmode_i": None, "results": results}


def _mismatch_events(caplog):
    return [
        record.rtl_fields
        for record in caplog.records
        if getattr(record, "rtl_event", None) == "dispatch.binary_mismatch"
    ]


def test_the_collect_audit_warns_when_one_key_produced_two_binaries(caplog):
    """One compile key is one binary; a mismatch is reported as a substitution.

    Runs gated on one build job validate the same stamp, so agreeing digests with
    disagreeing executables mean the shared directory was rebuilt while neighbours
    reused it. The audit only reports.
    """
    import logging as _logging

    rows = [
        _stamped_row("alpha", "k1", ["/b/simv", 10, 1], "/b"),
        _stamped_row("beta", "k1", ["/b/simv", 11, 2], "/b"),
        _stamped_row("gamma", "k2", ["/c/simv", 10, 1], "/c"),
        # Nothing to say: a run with no shared build at all.
        {
            "test_name": "delta",
            "randmode_i": None,
            "results": TestPassResults(name="delta/results"),
        },
    ]
    with caplog.at_level(_logging.WARNING):
        RtlBuddy._audit_shared_binaries(rows)

    events = _mismatch_events(caplog)
    assert len(events) == 1
    assert events[0]["build_dir"] == "/b"
    assert events[0]["fingerprints"] == 1
    assert events[0]["binaries"] == 2
    assert events[0]["tests"] == ["alpha", "beta"]


def test_the_collect_audit_still_groups_cache_mode_runs(caplog):
    """A persistent cache root moves the shared directory without changing what the
    audit keys on: agreeing runs stay silent and different binaries under one cache
    directory warn.
    """
    import logging as _logging

    cached = "/nfs/rb-cache/verif__blk/obj_dir_abc"
    with caplog.at_level(_logging.WARNING):
        RtlBuddy._audit_shared_binaries(
            [
                _stamped_row("alpha", "k1", ["simv", 10, 1], cached),
                _stamped_row("beta", "k1", ["simv", 10, 1], cached),
            ]
        )
    assert _mismatch_events(caplog) == []

    with caplog.at_level(_logging.WARNING):
        RtlBuddy._audit_shared_binaries(
            [
                _stamped_row("alpha", "k1", ["simv", 10, 1], cached),
                _stamped_row("beta", "k1", ["simv", 11, 2], cached),
            ]
        )
    events = _mismatch_events(caplog)
    assert len(events) == 1
    assert events[0]["build_dir"] == cached
    assert events[0]["tests"] == ["alpha", "beta"]


def test_the_collect_audit_groups_by_the_shared_directory_not_the_digest(caplog):
    """The audit keys on the compile directory, not the inputs' digest. An input edited
    mid-run and recompiled into the same directory changes both the digest and the
    binary, so keyed on the digest the two runs would fall into groups of one.
    """
    import logging as _logging

    rows = [
        _stamped_row("alpha", "k1", ["/b/simv", 10, 1], "/b"),
        _stamped_row("beta", "k1-edited", ["/b/simv", 11, 2], "/b"),
    ]
    with caplog.at_level(_logging.WARNING):
        RtlBuddy._audit_shared_binaries(rows)

    events = _mismatch_events(caplog)
    assert len(events) == 1
    assert events[0]["build_dir"] == "/b"
    assert events[0]["fingerprints"] == 2
    assert events[0]["binaries"] == 2
    assert events[0]["tests"] == ["alpha", "beta"]


def test_the_collect_audit_falls_back_to_the_digest_for_an_older_stamp(caplog):
    """An envelope without ``build_dir`` (from an older worker) groups on what it has."""
    import logging as _logging

    rows = [
        _stamped_row("alpha", "k1", ["/b/simv", 10, 1]),
        _stamped_row("beta", "k1", ["/b/simv", 11, 2]),
    ]
    with caplog.at_level(_logging.WARNING):
        RtlBuddy._audit_shared_binaries(rows)
    events = _mismatch_events(caplog)
    assert len(events) == 1
    assert events[0]["build_dir"] == "k1"


def test_the_collect_audit_is_silent_when_every_run_named_one_binary(caplog):
    """A healthy fan-out logs no warning."""
    import logging as _logging

    rows = [
        _stamped_row("alpha", "k1", ["/b/simv", 10, 1], "/b"),
        _stamped_row("beta", "k1", ["/b/simv", 10, 1], "/b"),
    ]
    with caplog.at_level(_logging.WARNING):
        RtlBuddy._audit_shared_binaries(rows)
    assert not _mismatch_events(caplog)


def test_the_collect_audit_skips_a_malformed_stamp_identity(caplog):
    """A malformed identity (a list where a path or digest belongs) counts as having
    nothing to say; the audit only reports and must not raise.
    """
    import logging as _logging

    rows = [
        _stamped_row("alpha", [], ["/b/simv", 10, 1], "/b"),
        _stamped_row("beta", "k1", ["/b/simv", 10, 1], []),
        _stamped_row("gamma", "k1", ["/b/simv", 11, 2], "/b"),
        _stamped_row("delta", "k1", ["/b/simv", 12, 3], "/b"),
    ]
    with caplog.at_level(_logging.WARNING):
        RtlBuddy._audit_shared_binaries(rows)
    events = _mismatch_events(caplog)
    assert len(events) == 1
    assert events[0]["tests"] == ["delta", "gamma"]


class _ReleasingBackend(_FakeBackend):
    """A fake that claims the Slurm backend name, the only backend whose pending jobs
    can be released.
    """

    name = "slurm"
    # Mirrors `SlurmDispatchBackend._sbatch_args_dependency()`: a dependency in
    # `sbatch-args` is appended after the generated `--dependency=afterok`, so it is the
    # sim job's effective gate.
    configured_dependency = None
    # An exported `$SBATCH_DEPENDENCY` is only a default for `-d`, which the command
    # line overrides, so it never gates the job.
    env_dependency = None

    def _sbatch_args_dependency(self):
        return self.configured_dependency

    def _configured_dependency(self):
        return self.configured_dependency or self.env_dependency

    def submit_array(self, specs, *, array_dir, max_parallel=None, dependency=None):
        return [self.submit(spec, dependency=dependency) for spec in specs]


def _log_records(log_path: Path) -> list[dict]:
    """Every record in a machine-mode rtl_buddy log, fields included."""
    if not log_path.exists():
        return []
    return [
        json.loads(line) for line in log_path.read_text().splitlines() if line.strip()
    ]


def _gates_manifest(project: Path) -> dict:
    paths = list(project.glob("artefacts/.dispatch/gates-*.json"))
    assert len(paths) == 1, [str(path) for path in paths]
    return json.loads(paths[0].read_text())


def test_head_records_every_submitted_job_against_its_plan_index(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The manifest names each key's jobs by plan index, the name the build job and the
    head share.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _ReleasingBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    manifest = _gates_manifest(minimal_project)
    assert manifest["schema_version"] == 1
    # Same index space as the plan beside it, and the same order.
    plans = list(minimal_project.glob("artefacts/.dispatch/plan-*.json"))
    planned = [test["name"] for test in json.loads(plans[0].read_text())["tests"]]
    assert [entry["test"] for entry in manifest["entries"]] == planned
    assert [entry["index"] for entry in manifest["entries"]] == list(
        range(len(planned))
    )
    assert [entry["job_id"] for entry in manifest["entries"]] == [
        "fake-1",
        "fake-2",
    ]
    # The token the build job checks the manifest's identity with; a manifest left at
    # this pid's path by an earlier head is refused.
    assert manifest["run_token"] == json.loads(plans[0].read_text())["run_token"]

    # The build job is told where to read it, beside its own envelope.
    spec = backend.build_submitted[0]
    assert spec.gates_json is not None
    assert Path(spec.gates_json).parent == Path(spec.result_json).parent

    # Every sim still carries the afterok that reaps the fan-out if the build job dies.
    assert backend.dependencies == ["fake-build", "fake-build"]


def test_head_writes_no_gates_manifest_for_a_backend_that_cannot_release(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """`local-parallel` has no pending queue, so it gets no manifest and its build job's
    argv is unchanged.
    """
    _mark_stub_builder_verilator(minimal_project)
    result, _rb = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    assert fake_backend.build_submitted[0].gates_json is None
    assert not list(minimal_project.glob("artefacts/.dispatch/gates-*.json"))


def test_head_writes_no_gates_manifest_without_a_build_job(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A suite whose tests each compile in their own job has no build job and is
    ungated.
    """
    backend = _ReleasingBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    assert backend.build_submitted == []
    assert not list(minimal_project.glob("artefacts/.dispatch/gates-*.json"))


def test_a_gates_manifest_that_cannot_be_written_does_not_fail_the_run(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The manifest is an optimization on a correct gate, so losing it costs only the
    early start and never the submitted fleet.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _ReleasingBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )

    def _boom(*_args, **_kwargs):
        raise OSError("Read-only file system")

    monkeypatch.setattr(rtl_buddy_module, "write_gates", _boom)
    result, _rb = _invoke(
        ["regression", "-c", "regression.yaml", "-l", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    assert backend.cancelled is False
    assert len(backend.submitted) == 2


def test_a_configured_dependency_turns_early_release_off_for_the_suite(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """`--dependency=singleton` in sbatch-args is the job's real gate.

    It is appended after the generated `afterok`, and `scontrol update Dependency=`
    clears an expression whole, so releasing a key would drop the site's serialisation.
    The suite gets no early release.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _ReleasingBackend()
    backend.configured_dependency = "singleton"
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    # --machine makes the head's log JSON lines, so the event is readable as a record.
    result, _rb = _invoke(
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

    # No manifest, no --gates: the build job never even probes for scontrol.
    assert backend.build_submitted[0].gates_json is None
    assert not list(minimal_project.glob("artefacts/.dispatch/gates-*.json"))
    # The run is otherwise untouched: every sim is submitted and gated on the build job.
    assert len(backend.submitted) == 2
    assert backend.dependencies == ["fake-build", "fake-build"]

    skipped = [
        record
        for record in _log_records(minimal_project / "rtl_buddy.log")
        if record.get("event") == "dispatch.gates_skipped"
    ]
    assert len(skipped) == 1, skipped
    assert skipped[0]["dependency"] == "singleton"
    assert "early release disabled" in skipped[0]["reason"]
    assert "sbatch-args" in skipped[0]["reason"]


def test_no_configured_dependency_leaves_early_release_on(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """With nothing configured, nothing is given up."""
    _mark_stub_builder_verilator(minimal_project)
    backend = _ReleasingBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _invoke(
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
    assert backend.build_submitted[0].gates_json is not None
    assert list(minimal_project.glob("artefacts/.dispatch/gates-*.json"))
    assert not [
        record
        for record in _log_records(minimal_project / "rtl_buddy.log")
        if record.get("event") == "dispatch.gates_skipped"
    ]


def test_the_gates_skipped_event_has_a_dedicated_human_message():
    from rtl_buddy.logging_utils import _human_message

    message = _human_message(
        "dispatch.gates_skipped",
        {
            "suite_dir": "/w/verif/blk",
            "dependency": "singleton",
            "reason": "early release disabled: sbatch-args/SBATCH_DEPENDENCY "
            "configures a dependency (singleton) that a release would clear",
        },
    )
    assert "/w/verif/blk" in message and "singleton" in message
    assert "waits for its build job" in message
    assert "dispatch gates_skipped" not in message


def test_a_partial_build_envelope_is_used_for_what_it_names_only(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A build job that released a key and then died.

    Its envelope exists but stops at the key it reached. `basic` compiled and its jobs
    ran, so its row is the build job's verdict; `extra` was never compiled, which is the
    same as no envelope. The file's existence is not read as the build having finished.
    """
    _mark_stub_builder_verilator(minimal_project)

    class _PartialBuild(_FakeBackend):
        def __init__(self):
            super().__init__(write_results=False)  # no sim envelope appears

        def submit_build(self, spec, *, dependency=None):
            write_build_result_json(
                spec.result_json,
                built=[],
                failed=["basic"],
                builds=[
                    {
                        "test": "basic",
                        "builder": "verilator",
                        "returncode": 1,
                        "error_tail": ["%Error: Exiting due to 1 error(s)"],
                    }
                ],
                partial=True,
            )
            return super().submit_build(spec, dependency=dependency)

    backend = _PartialBuild()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _invoke(
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
    assert result.exit_code == 1, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    rows = {r["name"]: r for r in json.loads(payload_line)["payload"]["results"]}
    # The record it DOES hold is used, exactly as a complete one would be.
    assert "compile failed in build job" in rows["basic"]["desc"]
    # The test it never reached falls back to the missing-result story.
    assert "produced no result" in rows["extra"]["desc"]

    partial = [
        record
        for record in _log_records(minimal_project / "rtl_buddy.log")
        if record.get("event") == "dispatch.build_result_partial"
    ]
    assert len(partial) == 1, partial
    assert partial[0]["decided"] == 1
    assert partial[0]["planned"] == 2


def test_a_partial_build_envelope_does_not_feed_reservation_advice(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A partial envelope's records are a fraction of the paid-for compiles, so no
    compile-block advice is given.
    """
    _mark_stub_builder_verilator(minimal_project)

    class _PartialBuild(_FakeBackend):
        def submit_build(self, spec, *, dependency=None):
            write_build_result_json(
                spec.result_json,
                built=["basic"],
                failed=[],
                builds=[{"test": "basic", "builder": "verilator", "duration_sec": 2.0}],
                partial=True,
            )
            return super().submit_build(spec, dependency=dependency)

    backend = _PartialBuild()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    states = []
    original = rtl_buddy_module.RtlBuddy._dispatch_collect

    def _spy(self, backend_arg, state, *args, **kwargs):
        out = original(self, backend_arg, state, *args, **kwargs)
        states.append(state)
        return out

    monkeypatch.setattr(rtl_buddy_module.RtlBuddy, "_dispatch_collect", _spy)
    result, _rb = _invoke(
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
    assert states and states[0]["build_compile_work"] is None


def test_the_partial_envelope_events_have_dedicated_human_messages():
    from rtl_buddy.logging_utils import _human_message

    head = _human_message(
        "dispatch.build_result_partial",
        {
            "suite_dir": "/w/verif/blk",
            "job_id": "1234",
            "decided": 1,
            "planned": 4,
        },
    )
    assert "1234" in head and "/w/verif/blk" in head and "did not finish" in head
    assert "dispatch build_result_partial" not in head

    job = _human_message(
        "build_job.partial_result_failed",
        {
            "path": "/w/.dispatch/build-result-7.json",
            "error": "[Errno 28] No space left on device",
        },
    )
    assert "build-result-7.json" in job and "No space left" in job
    assert "build_job partial_result_failed" not in job


def test_a_partial_envelope_counts_configs_not_result_rows(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """The partial-envelope count is in configs, not rows: one config over a hundred
    runs is not "1 of 100".
    """

    class _PartialBuild(_RecordingBackend):
        def submit_build(self, spec, *, dependency=None):
            handle = _FakeBackend.submit_build(self, spec)
            write_build_result_json(
                spec.result_json,
                built=["basic"],
                failed=[],
                builds=[{"test": "basic", "builder": "verilator"}],
                partial=True,
            )
            return handle

    backend = _PartialBuild()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _invoke(
        ["--machine", "randtest", "basic", "5", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    # One config, five runs.
    assert [spec.run_id for spec in backend.submitted] == [1, 2, 3, 4, 5]

    partial = [
        record
        for record in _log_records(minimal_project / "rtl_buddy.log")
        if record.get("event") == "dispatch.build_result_partial"
    ]
    # Once per suite, whatever the fan-out, and in configs on both sides.
    assert len(partial) == 1, partial
    assert partial[0]["decided"] == 1
    assert partial[0]["planned"] == 1


def test_an_exported_dependency_does_not_turn_early_release_off(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """`$SBATCH_DEPENDENCY` is a default, not a gate, on a gated job.

    A command-line `-d` overrides the export, and every gated job carries a generated
    `--dependency=afterok`. Clearing it drops nothing the site configured, so the suite
    keeps its early release.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _ReleasingBackend()
    backend.env_dependency = "afterok:9"
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _invoke(
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

    assert backend.build_submitted[0].gates_json is not None
    assert list(minimal_project.glob("artefacts/.dispatch/gates-*.json"))
    records = _log_records(minimal_project / "rtl_buddy.log")
    assert not [r for r in records if r.get("event") == "dispatch.gates_skipped"]
    overridden = [
        r for r in records if r.get("event") == "dispatch.env_dependency_overridden"
    ]
    assert len(overridden) == 1, overridden
    assert overridden[0]["dependency"] == "afterok:9"


def test_sbatch_args_wins_over_an_export_when_both_are_set(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The command-line half decides, as it does for sbatch itself."""
    _mark_stub_builder_verilator(minimal_project)
    backend = _ReleasingBackend()
    backend.configured_dependency = "singleton"
    backend.env_dependency = "afterok:9"
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _invoke(
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
    assert backend.build_submitted[0].gates_json is None
    records = _log_records(minimal_project / "rtl_buddy.log")
    skipped = [r for r in records if r.get("event") == "dispatch.gates_skipped"]
    assert [r["dependency"] for r in skipped] == ["singleton"]
    # One answer, not two: the export is not also reported as ignored.
    assert not [
        r for r in records if r.get("event") == "dispatch.env_dependency_overridden"
    ]


class _PartialEnvelopeBackend(_RecordingBackend):
    """A build job that leaves a partial envelope naming only `basic`.

    ``build_state`` is what the backend reports, which distinguishes a job that died
    mid-compile from one that finished and lost the final write. ``via`` chooses the
    backend half that answers: ``"telemetry"`` (Slurm, an sacct row) or ``"outcome"``
    (local-parallel, via `build_outcome`).
    """

    def __init__(self, build_state, via="telemetry"):
        super().__init__(write_results=False)  # no sim envelope appears
        self._build_state = build_state
        if via == "telemetry":
            self.telemetry = {"fake-build": {"state": build_state, "elapsed_s": 5}}
        else:
            self.telemetry = {}

    def build_outcome(self, handle):
        return self._build_state

    def submit_build(self, spec, *, dependency=None):
        handle = _FakeBackend.submit_build(self, spec)
        write_build_result_json(
            spec.result_json,
            built=["basic"],
            failed=[],
            builds=[{"test": "basic", "builder": "verilator", "duration_sec": 2.0}],
            partial=True,
        )
        return handle


def _run_partial_envelope(
    minimal_project, monkeypatch, build_state, *, retry=False, via="telemetry"
):
    _mark_stub_builder_verilator(minimal_project)
    if retry:
        # The gate only shows through the retry classifier, the one consumer of
        # `build_succeeded`.
        _enable_retry(minimal_project)
    backend = _PartialEnvelopeBackend(build_state, via=via)
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    states = []
    original = rtl_buddy_module.RtlBuddy._dispatch_collect

    def _spy(self, backend_arg, state, *args, **kwargs):
        out = original(self, backend_arg, state, *args, **kwargs)
        states.append(state)
        return out

    monkeypatch.setattr(rtl_buddy_module.RtlBuddy, "_dispatch_collect", _spy)
    gates = {}
    real_classify = rtl_buddy_module.classify_missing_result

    def _classify(spec, sched_state, **kwargs):
        gates[spec.test_name] = kwargs.get("build_succeeded")
        return real_classify(spec, sched_state, **kwargs)

    monkeypatch.setattr(rtl_buddy_module, "classify_missing_result", _classify)
    result, _rb = _invoke(
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
    return result, states, _log_records(minimal_project / "rtl_buddy.log"), gates


def test_a_partial_envelope_from_a_killed_build_job_closes_the_unnamed_gates(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """An unknown build state is read conservatively.

    A killed build job never reached the tests its envelope does not name; their jobs
    were cancelled, so a retry would resubmit them ungated.
    """
    result, states, records, gates = _run_partial_envelope(
        minimal_project, monkeypatch, "TIMEOUT", retry=True
    )
    assert result.exit_code == 1, result.output

    # `basic` is named, so its gate opened; `extra` is not, so its gate stayed shut and
    # its missing result is not retried.
    assert gates == {"basic": True, "extra": False}, gates

    partial = [r for r in records if r.get("event") == "dispatch.build_result_partial"]
    assert len(partial) == 1, partial
    assert partial[0]["scheduler_state"] == "TIMEOUT"
    assert not [
        r for r in records if r.get("event") == "dispatch.build_result_final_write_lost"
    ]
    # `extra`'s gate stayed shut, so its missing result is not retryable.
    assert states[0]["build_compile_work"] is None


def test_a_partial_envelope_from_a_completed_build_job_is_a_lost_final_write(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """COMPLETED means the build job reached every test and only the last write was
    lost, so missing results are classified normally.

    Advice stays suppressed because the records are still a fraction of the compiles.
    """
    result, states, records, gates = _run_partial_envelope(
        minimal_project, monkeypatch, "COMPLETED", retry=True
    )
    assert result.exit_code == 1, result.output

    # Every planned test's gate is open, so a missing result is ordinary and classified
    # as one.
    assert gates == {"basic": True, "extra": True}, gates

    lost = [
        r for r in records if r.get("event") == "dispatch.build_result_final_write_lost"
    ]
    assert len(lost) == 1, lost
    assert lost[0]["decided"] == 1 and lost[0]["planned"] == 2
    assert not [r for r in records if r.get("event") == "dispatch.build_result_partial"]
    # Advice is still dropped: the records are a fraction of the compiles.
    assert states[0]["build_compile_work"] is None


def test_the_final_write_lost_event_has_a_dedicated_human_message():
    from rtl_buddy.logging_utils import _human_message

    message = _human_message(
        "dispatch.build_result_final_write_lost",
        {
            "suite_dir": "/w/verif/blk",
            "job_id": "1234",
            "decided": 1,
            "planned": 4,
        },
    )
    assert "1234" in message and "/w/verif/blk" in message
    assert "finished" in message and "lost" in message
    assert "dispatch build_result_final_write_lost" not in message

    killed = _human_message(
        "dispatch.build_result_partial",
        {
            "suite_dir": "/w/verif/blk",
            "job_id": "1234",
            "decided": 1,
            "planned": 4,
            "scheduler_state": "TIMEOUT",
        },
    )
    assert "TIMEOUT" in killed and "did not finish" in killed

    overridden = _human_message(
        "dispatch.env_dependency_overridden",
        {"suite_dir": "/w/verif/blk", "dependency": "afterok:9"},
    )
    assert "SBATCH_DEPENDENCY" in overridden and "afterok:9" in overridden
    assert "sbatch-args" in overridden
    assert "dispatch env_dependency_overridden" not in overridden


def test_a_backend_without_accounting_still_tells_the_two_readings_apart(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """`local-parallel` has no telemetry, so the head asks `build_outcome` for the build
    process's exit status.

    `collect_telemetry()` returns `{}`, so reading the sacct state alone would treat
    every partial envelope as a dead build job and refuse valid retries.
    """
    result, states, records, gates = _run_partial_envelope(
        minimal_project, monkeypatch, "COMPLETED", retry=True, via="outcome"
    )
    assert result.exit_code == 1, result.output

    lost = [
        r for r in records if r.get("event") == "dispatch.build_result_final_write_lost"
    ]
    assert len(lost) == 1, lost
    assert gates == {"basic": True, "extra": True}, gates
    assert states[0]["build_compile_work"] is None


def test_a_backend_without_accounting_keeps_the_conservative_reading_on_failure(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A build process that exited nonzero never reached the rest."""
    result, states, records, gates = _run_partial_envelope(
        minimal_project, monkeypatch, "FAILED", retry=True, via="outcome"
    )
    assert result.exit_code == 1, result.output

    partial = [r for r in records if r.get("event") == "dispatch.build_result_partial"]
    assert len(partial) == 1, partial
    assert partial[0]["scheduler_state"] == "FAILED"
    assert gates == {"basic": True, "extra": False}, gates


def test_a_backend_that_cannot_say_keeps_the_conservative_reading(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """`None` from both halves is "unknown", and unknown stays cautious."""
    result, _states, records, gates = _run_partial_envelope(
        minimal_project, monkeypatch, None, retry=True, via="outcome"
    )
    assert result.exit_code == 1, result.output
    assert [
        r["event"]
        for r in records
        if r.get("event", "").startswith("dispatch.build_result")
    ] == ["dispatch.build_result_partial"]
    assert gates == {"basic": True, "extra": False}, gates


class _OrphanBackend(_ReleasingBackend):
    """A scheduler-backed fake that can be told what is still queued.

    ``live`` is the set of job ids the fake scheduler still holds; the manifest supplies
    the ids and the backend says which are real.
    """

    def __init__(self, live=(), cancel_works=True, **kwargs):
        super().__init__(**kwargs)
        self.live = set(live)
        self.probed = []
        self.probe_timeouts = []
        self.cancelled_handles = []
        # Where the head told each array to keep its manifest and scripts.
        self.array_dirs = []
        # `cancel_works` False models a `scancel` that did not take, which `cancel_all`
        # cannot report because it is best effort.
        self.cancel_works = cancel_works

    def submit_array(self, specs, *, array_dir, max_parallel=None, dependency=None):
        self.array_dirs.append(Path(array_dir))
        return super().submit_array(
            specs,
            array_dir=array_dir,
            max_parallel=max_parallel,
            dependency=dependency,
        )

    def live_job_ids(self, handles, *, timeout_s=None):
        self.probed.append([handle.job_id for handle in handles])
        self.probe_timeouts.append(timeout_s)
        return {handle.job_id for handle in handles if handle.job_id in self.live}

    def cancel_all(self, handles):
        self.cancelled_handles.append([handle.job_id for handle in handles])
        if self.cancel_works:
            self.live -= {handle.job_id for handle in handles if handle is not None}
        super().cancel_all(handles)


class _PoolBackend(_ReleasingBackend):
    """A fake whose jobs are the head's own children (`local-parallel`)."""

    name = "local-parallel"
    scheduled = False


def _run_manifests(project: Path) -> list[Path]:
    return sorted(project.glob("artefacts/.dispatch/run-*.json"))


def _run_manifest(project: Path) -> dict:
    paths = _run_manifests(project)
    assert len(paths) == 1, [str(path) for path in paths]
    return json.loads(paths[0].read_text())


def _orphan_the_run(project: Path) -> tuple[Path, dict]:
    """Rewind this project's manifest to what a killed head leaves behind.

    Only the status changes. Both runs share this process's pid, so this exercises the
    token-keyed manifest name: the run token is per invocation and the pid is not.
    """
    paths = _run_manifests(project)
    assert len(paths) == 1, [str(path) for path in paths]
    payload = json.loads(paths[0].read_text())
    payload["status"] = "running"
    paths[0].write_text(json.dumps(payload))
    return paths[0], payload


def _all_job_ids(payload) -> list[str]:
    ids = [entry["job_id"] for entry in payload["pending"]]
    # Both compile jobs when the compile was split: an orphan's verilate job is as much
    # a survivor as its build job.
    for key in ("build", "verilate"):
        if payload.get(key) is not None:
            ids.append(payload[key]["job_id"])
    return ids


def _dispatched_regression(argv=()):
    """One dispatched regression, with the artefact-tree lock handed back.

    The tree lock is released when the RtlBuddy that took it is collected; without this,
    the second invocation in one process would refuse to start.
    """
    result, rb = _invoke(
        [
            "regression",
            "-c",
            "regression.yaml",
            "-l",
            "5",
            "--dispatch",
            "slurm",
            *argv,
        ]
    )
    rb._artifact_locks.release_all()
    return result, rb


def _console_text(result) -> str:
    """The console output with line breaks removed, so assertions do not depend on
    Rich's wrapping.
    """
    return " ".join(result.output.split())


def _logged_events(project: Path, event: str) -> list[dict]:
    """Every occurrence of one event in the project's head logs.

    Only usable for an event logged by the last thing a suite does: the head re-anchors
    and rewrites the suite's file log before collecting.
    """
    found = []
    for log in sorted(project.rglob("rtl_buddy.log")):
        for line in log.read_text().splitlines():
            if not line.strip().startswith("{"):
                continue
            record = json.loads(line)
            if record.get("event") == event:
                found.append(record)
    return found


def _fatal_text(result) -> str:
    """The message of a fatal raised out of `rb.app`; `_invoke` bypasses
    `RtlBuddy.run()`, so the text is on the exception, not in the output.
    """
    assert result.exception is not None, result.output
    return str(result.exception)


def test_head_records_its_whole_fleet_in_a_run_manifest(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The record a dead head leaves for the next one: the job ids, the specs to rebuild
    their handles and the run token their envelopes are stamped with.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _dispatched_regression()
    assert result.exit_code == 0, result.output

    manifest = _run_manifest(minimal_project)
    assert manifest["schema_version"] == 1
    assert manifest["backend"] == "slurm"
    assert manifest["pid"] == os.getpid()
    # A collected run is settled: the next invocation never probes it.
    assert manifest["status"] == "collected"

    plans = list(minimal_project.glob("artefacts/.dispatch/plan-*.json"))
    plan = json.loads(plans[0].read_text())
    assert manifest["run_token"] == plan["run_token"]
    assert manifest["plan"] == str(plans[0])
    assert Path(manifest["suite_config"]).name == "tests.yaml"

    # Every submitted row, against the row index the collector fills in.
    assert [entry["job_id"] for entry in manifest["pending"]] == ["fake-1", "fake-2"]
    assert [entry["row"] for entry in manifest["pending"]] == [0, 1]
    assert manifest["build"]["job_id"] == "fake-build"
    assert manifest["build"]["spec"]["kind"] == "build"
    # The sim spec carries what a collector needs to rebuild the handle: the envelope
    # path and the plan.
    spec = manifest["pending"][0]["spec"]
    assert spec["kind"] == "test"
    assert spec["result_json"].endswith(".json")
    assert spec["plan_path"] == str(plans[0])
    assert [row["test_name"] for row in manifest["rows"]] == [
        entry["spec"]["test_name"] for entry in manifest["pending"]
    ]
    # Placeholders and live result objects alike stay out of the record.
    assert all("results" not in row for row in manifest["rows"])
    assert manifest["submitted_at"] >= manifest["started_at"]


def test_head_marks_the_run_manifest_cancelled_when_it_takes_the_fleet_down(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A cancelled fleet is not an orphan on the next run: the head that cancels marks
    the manifest settled.
    """
    _mark_stub_builder_verilator(minimal_project)

    class _FailingWait(_OrphanBackend):
        def wait_all(self, handles, *, extra_wait=0.0):
            raise RuntimeError("controller unreachable")

    backend = _FailingWait()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _dispatched_regression()
    assert result.exit_code != 0
    assert backend.cancelled
    assert _run_manifest(minimal_project)["status"] == "cancelled"


def test_head_writes_no_run_manifest_for_jobs_that_die_with_it(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """`local-parallel` jobs are this process's children, so an interrupted run leaves
    nothing to find or record.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _PoolBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _invoke(
        [
            "regression",
            "-c",
            "regression.yaml",
            "-l",
            "5",
            "--dispatch",
            "local-parallel",
        ]
    )
    assert result.exit_code == 0, result.output
    assert not _run_manifests(minimal_project)


def test_discovery_skips_this_runs_own_token_and_a_settled_manifest(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A manifest carrying this run's token, and one whose run already ended, are never
    orphans.

    The token excludes a head's own record, not the pid: a regression writes one
    manifest per suite under one token, and the OS reuses pids.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _dispatched_regression()
    assert result.exit_code == 0, result.output
    assert backend.probed == []  # nothing to probe on a first run

    dispatch_root = minimal_project / "artefacts" / ".dispatch"
    manifest_path, payload = _orphan_the_run(minimal_project)
    token = payload["run_token"]
    # Its own token: not an orphan, however live its jobs look.
    assert discover_run_manifests(dispatch_root, run_token=token) == []
    # Anybody else's: found, even though the pid is this very process's.
    found = discover_run_manifests(dispatch_root, run_token="some-other-run")
    assert [path for path, _payload in found] == [manifest_path]
    assert payload["pid"] == os.getpid()

    payload["status"] = "collected"
    manifest_path.write_text(json.dumps(payload))
    assert discover_run_manifests(dispatch_root, run_token="some-other-run") == []
    result, _rb = _dispatched_regression()
    assert result.exit_code == 0, result.output
    assert backend.probed == []


def test_a_reused_pid_neither_hides_nor_overwrites_an_orphan(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A pid is not a name.

    After a reboot a head can carry the pid of a run whose fleet is still queued. The
    manifest is keyed on the run token so the new record does not overwrite the old one,
    and the old one is found rather than excluded by pid. Both runs here share this
    process's pid.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0
    manifest_path, payload = _orphan_the_run(minimal_project)
    assert payload["pid"] == os.getpid()
    backend.live = set(_all_job_ids(payload))

    result, _rb = _dispatched_regression()
    assert result.exit_code == 0, result.output
    # Found, not hidden by the shared pid...
    assert "still queued or running" in _console_text(result)
    assert json.loads(manifest_path.read_text())["status"] == "running"
    paths = _run_manifests(minimal_project)
    assert len(paths) == 2, [str(path) for path in paths]
    tokens = {json.loads(path.read_text())["run_token"] for path in paths}
    assert len(tokens) == 2
    # The token is in the filename, which is what keeps them apart.
    assert {path.name for path in paths} == {
        f"run-{os.getpid()}-{json.loads(path.read_text())['run_token'][:8]}.json"
        for path in paths
    }
    assert manifest_path.name.endswith(f"{payload['run_token'][:8]}.json")


def test_discovery_retires_a_manifest_whose_jobs_have_all_finished(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A head killed after its fleet finished leaves a manifest with nothing behind it;
    it is marked stale and probed once.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0
    manifest_path, _payload = _orphan_the_run(minimal_project)

    result, _rb = _dispatched_regression()
    assert result.exit_code == 0, result.output
    assert backend.probed, "the manifest was never put to the backend"
    assert json.loads(manifest_path.read_text())["status"] == "stale"
    assert "still queued or running" not in result.output


def test_warn_names_the_orphaned_jobs_and_submits_anyway(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The default: the jobs are named and the run proceeds unchanged."""
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0
    manifest_path, payload = _orphan_the_run(minimal_project)
    orphan_ids = _all_job_ids(payload)
    backend.live = set(orphan_ids)
    submitted_before = len(backend.submitted)

    result, _rb = _dispatched_regression()
    assert result.exit_code == 0, result.output
    console = _console_text(result)
    assert "still queued or running" in console
    for job_id in orphan_ids:
        assert job_id in console
    assert "--orphans adopt" in console
    assert "--orphans cancel" in console
    assert len(backend.submitted) > submitted_before
    assert backend.cancelled_handles == []
    # Untouched: `warn` reports, it does not decide.
    assert json.loads(manifest_path.read_text())["status"] == "running"


def test_cancel_scancels_the_orphaned_fleet_then_submits_a_fresh_one(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """`--orphans cancel` takes the old fleet down before this one goes out."""
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0
    manifest_path, payload = _orphan_the_run(minimal_project)
    orphan_ids = _all_job_ids(payload)
    backend.live = set(orphan_ids)
    submitted_before = len(backend.submitted)

    result, _rb = _dispatched_regression(["--orphans", "cancel"])
    assert result.exit_code == 0, result.output
    # Exactly the orphan's handles, rebuilt from its manifest.
    assert len(backend.cancelled_handles) == 1
    assert sorted(backend.cancelled_handles[0]) == sorted(orphan_ids)
    assert json.loads(manifest_path.read_text())["status"] == "cancelled"
    assert len(backend.submitted) > submitted_before
    console = _console_text(result)
    # Four: two sim jobs plus the compile's two chained halves.
    assert "cancelled 4 job(s) left by an earlier run" in console
    for job_id in orphan_ids:
        assert job_id in console


def test_adopt_collects_the_orphaned_fleet_and_submits_nothing(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """`--orphans adopt` collects the orphan's fleet by the orphan's run token and
    submits nothing.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0
    manifest_path, payload = _orphan_the_run(minimal_project)
    backend.live = set(_all_job_ids(payload))
    submitted_before = list(backend.submitted)
    builds_before = list(backend.build_submitted)

    result, _rb = _invoke(
        [
            "--machine",
            "regression",
            "-c",
            "regression.yaml",
            "-l",
            "5",
            "--dispatch",
            "slurm",
            "--orphans",
            "adopt",
        ]
    )
    assert result.exit_code == 0, result.output
    assert backend.submitted == submitted_before
    assert backend.build_submitted == builds_before
    # No second plan: the adopted jobs read the one their own head wrote.
    assert len(list(minimal_project.glob("artefacts/.dispatch/plan-*.json"))) == 1

    envelope = json.loads(
        [line for line in result.output.splitlines() if line.startswith("{")][-1]
    )
    results = {row["name"]: row["result"] for row in envelope["payload"]["results"]}
    assert set(results.values()) == {"PASS"}, results
    assert json.loads(manifest_path.read_text())["status"] == "collected"
    adopted = _logged_events(minimal_project, "dispatch.orphans_adopted")
    assert len(adopted) == 1, adopted
    assert adopted[0]["run_token"] == payload["run_token"]
    assert adopted[0]["build_job"] == payload["build"]["job_id"]


def test_adopt_refuses_a_fleet_that_ran_a_different_test_set(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Adopting a fleet planned from other tests is fatal and says what differs."""
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0
    manifest_path, payload = _orphan_the_run(minimal_project)
    backend.live = set(_all_job_ids(payload))
    # Rewrite the record so it names a test this invocation does not plan.
    payload["rows"][0]["test_name"] = "some_other_test"
    manifest_path.write_text(json.dumps(payload))

    submitted_before = list(backend.submitted)
    result, _rb = _dispatched_regression(["--orphans", "adopt"])
    assert result.exit_code != 0
    message = _fatal_text(result)
    assert "cannot adopt" in message
    assert "some_other_test" in message
    assert "--orphans cancel" in message
    assert backend.submitted == submitted_before  # nothing new went out
    assert json.loads(manifest_path.read_text())["status"] == "running"


def test_adopt_refuses_a_fleet_planned_with_different_plusdefines(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Same test names, different simulation: adoption is refused.

    A changed plusdefine compiles a different design. The fresh expansion is compared
    against the orphan's own plan manifest, so the refusal names the field.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0
    manifest_path, payload = _orphan_the_run(minimal_project)
    backend.live = set(_all_job_ids(payload))
    submitted_before = list(backend.submitted)

    # The first `plusdefines:` in the fixture belongs to `basic`.
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text().replace(
            "    plusdefines:\n", "    plusdefines:\n      WIDTH: 8\n", 1
        )
    )

    result, _rb = _dispatched_regression(["--orphans", "adopt"])
    assert result.exit_code != 0
    message = _fatal_text(result)
    assert "cannot adopt" in message
    # The row identities match, so only the plan comparison catches this.
    assert "'basic'" in message
    assert "'pd'" in message
    assert "WIDTH" in message
    assert backend.submitted == submitted_before
    assert json.loads(manifest_path.read_text())["status"] == "running"


def test_adopt_refuses_a_fleet_planned_with_a_different_master_seed(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A re-run that pins a different master seed is a different run and is refused."""
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0
    manifest_path, payload = _orphan_the_run(minimal_project)
    backend.live = set(_all_job_ids(payload))
    submitted_before = list(backend.submitted)

    result, _rb = _dispatched_regression(["--orphans", "adopt", "--master-seed", "77"])
    assert result.exit_code != 0
    message = _fatal_text(result)
    assert "master seed" in message
    assert "77" in message
    assert backend.submitted == submitted_before
    assert json.loads(manifest_path.read_text())["status"] == "running"


def test_adopt_refuses_an_unreadable_plan_rather_than_guessing(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Without the orphan's plan there is nothing to match on, so adoption is refused."""
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0
    manifest_path, payload = _orphan_the_run(minimal_project)
    backend.live = set(_all_job_ids(payload))
    Path(payload["plan"]).unlink()

    result, _rb = _dispatched_regression(["--orphans", "adopt"])
    assert result.exit_code != 0
    assert "cannot be read" in _fatal_text(result)
    assert json.loads(manifest_path.read_text())["status"] == "running"


def test_cancel_refuses_to_submit_beside_a_fleet_it_could_not_take_down(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """`cancel_all` is best effort, so `cancel` verifies.

    A refused `scancel`, or an unreachable `squeue` (which reports the recorded ids
    live), stops the run rather than submitting a second fleet beside one still holding
    the cluster.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend(cancel_works=False)
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    # No grace period: the first re-probe is the verdict.
    monkeypatch.setattr(RtlBuddy, "ORPHAN_CANCEL_WAIT_S", 0.0)
    assert _dispatched_regression()[0].exit_code == 0
    manifest_path, payload = _orphan_the_run(minimal_project)
    orphan_ids = _all_job_ids(payload)
    backend.live = set(orphan_ids)
    submitted_before = list(backend.submitted)

    result, _rb = _dispatched_regression(["--orphans", "cancel"])
    assert result.exit_code != 0
    message = _fatal_text(result)
    assert "could not take down" in message
    for job_id in orphan_ids:
        assert job_id in message
    assert "--orphans adopt" in message
    # Nothing submitted, and the record still says the fleet is out there.
    assert backend.submitted == submitted_before
    assert json.loads(manifest_path.read_text())["status"] == "running"
    console = _console_text(result)
    assert "after scancel" in console


def test_a_record_for_another_config_is_not_this_suites_orphan(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The scan spans a suite's whole `.dispatch/` tree and keeps only this suite's own
    records; a neighbour's fleet is not this suite's to report, cancel or adopt.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0
    manifest_path, payload = _orphan_the_run(minimal_project)
    backend.live = set(_all_job_ids(payload))
    payload["suite_config"] = str(minimal_project / "other-tests.yaml")
    manifest_path.write_text(json.dumps(payload))

    # Not ours: nothing is probed, nothing is reported...
    result, _rb = _dispatched_regression()
    assert result.exit_code == 0, result.output
    assert backend.probed == []
    assert "still queued or running" not in _console_text(result)
    result, _rb = _dispatched_regression(["--orphans", "adopt"])
    assert result.exit_code != 0
    assert "found no interrupted run" in _fatal_text(result)
    assert json.loads(manifest_path.read_text())["status"] == "running"


def test_adopt_refuses_to_choose_between_two_orphaned_runs(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Two interrupted runs with live jobs have no right answer: adopting one would
    abandon the other's fleet.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0
    manifest_path, payload = _orphan_the_run(minimal_project)
    backend.live = set(_all_job_ids(payload))
    second = manifest_path.with_name("run-999999.json")
    twin = dict(payload, pid=999999, run_token="a-second-run")
    second.write_text(json.dumps(twin))

    result, _rb = _dispatched_regression(["--orphans", "adopt"])
    assert result.exit_code != 0
    message = _fatal_text(result)
    assert "found 2 interrupted runs" in message
    assert "--orphans cancel" in message


def test_adopt_refuses_a_backend_whose_jobs_died_with_their_head(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Nothing survives a `local-parallel` head, so `adopt` there cannot be honoured and
    is refused.
    """
    _mark_stub_builder_verilator(minimal_project)
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(_PoolBackend())
    )
    result, _rb = _invoke(
        [
            "regression",
            "-c",
            "regression.yaml",
            "-l",
            "5",
            "--dispatch",
            "local-parallel",
            "--orphans",
            "adopt",
        ]
    )
    assert result.exit_code != 0
    assert "nothing to adopt" in _fatal_text(result)
    assert "--dispatch slurm" in _fatal_text(result)


def test_an_unknown_orphans_policy_is_rejected(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    _mark_stub_builder_verilator(minimal_project)
    result, _rb = _dispatched_regression(["--orphans", "collect"])
    assert result.exit_code != 0
    assert "--orphans must be one of" in _fatal_text(result)


class _ManifestWatchingRetryBackend(_RetryBackend):
    """A retry fleet that reads the run manifest at every ``wait_all``, where a head
    spends the round.
    """

    name = "slurm"

    def __init__(self, project, **kwargs):
        super().__init__(**kwargs)
        self._project = project
        self.pending_at_wait = []

    def wait_all(self, handles, *, extra_wait=0.0):
        paths = sorted(self._project.glob("artefacts/.dispatch/run-*.json"))
        self.pending_at_wait.append(
            [entry["job_id"] for entry in json.loads(paths[0].read_text())["pending"]]
            if paths
            else None
        )
        super().wait_all(handles, extra_wait=extra_wait)


def test_a_retry_round_is_recorded_before_the_head_waits_on_it(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A head killed mid-retry leaves an adoptable record.

    `_resubmit_retryable` blocks until the round drains, so the manifest must name the
    retry's jobs before the wait, not after it returns.
    """
    _enable_retry(minimal_project)
    backend = _use_backend(
        monkeypatch,
        _ManifestWatchingRetryBackend(minimal_project, passes_on_attempt=2),
    )

    result, _rb = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output
    assert _rows(result)["basic"]["result"] == "PASS"
    assert backend.wait_calls == 2
    assert backend.pending_at_wait == [["fake-1"], ["fake-2"]]
    manifest = _run_manifest(minimal_project)
    assert [entry["job_id"] for entry in manifest["pending"]] == ["fake-2"]
    assert manifest["status"] == "collected"


def test_a_recorded_job_spec_rebuilds_exactly(tmp_path: Path):
    """The manifest survives JSON: paths, enums and the nested reservation come back as
    the types a collector uses.
    """
    from rtl_buddy.dispatch.run_manifest import decode_spec, encode_spec

    spec = SimJobSpec(
        test_name="basic",
        suite_dir=str(tmp_path),
        test_config_path=str(tmp_path / "tests.yaml"),
        result_json=tmp_path / "result.json",
        run_id=3,
        seed_mode=SeedMode.NEW,
        master_seed=99,
        resolved_seed=7,
        expect_prebuilt=True,
        build_result_json=tmp_path / "build-result.json",
        log_path=tmp_path / "slurm.log",
        plan_path=tmp_path / "plan.json",
    )
    assert decode_spec(encode_spec(spec)) == spec

    build = rtl_buddy_module.BuildJobSpec(
        suite_dir=str(tmp_path),
        test_config_path=str(tmp_path / "tests.yaml"),
        parallel=3,
        rebuild=True,
        plan_path=tmp_path / "plan.json",
        result_json=tmp_path / "build-result.json",
        gates_json=tmp_path / "gates.json",
    )
    assert decode_spec(encode_spec(build)) == build


def test_a_manifest_from_another_version_is_skipped_not_fatal(tmp_path: Path):
    """A run that has submitted nothing is not refused because of a file a neighbouring
    rtl_buddy wrote.
    """
    from rtl_buddy.dispatch.run_manifest import load_run_manifest

    (tmp_path / "run-1.json").write_text(json.dumps({"schema_version": 99}))
    (tmp_path / "run-2.json").write_text("{ not json")
    payload, reason = load_run_manifest(tmp_path / "run-1.json")
    assert payload is None and "schema_version" in reason
    assert discover_run_manifests(tmp_path, run_token="t") == []


class _DyingBackend(_OrphanBackend):
    """A head killed mid-fan-out: the raise at the Nth array submission stands in for
    SIGKILL.

    The submissions accepted before it are running jobs, and the record on disk is the
    only thing that can name them.
    """

    def __init__(self, die_on_array=1, **kwargs):
        super().__init__(**kwargs)
        self.die_on_array = die_on_array
        self.arrays = 0

    def submit_array(self, specs, *, array_dir, max_parallel=None, dependency=None):
        self.arrays += 1
        if self.arrays >= self.die_on_array:
            raise RuntimeError(f"head died before array {self.arrays}")
        return super().submit_array(
            specs,
            array_dir=array_dir,
            max_parallel=max_parallel,
            dependency=dependency,
        )


def _split_resource_groups(project: Path):
    """Give `extra` its own reservation, so the suite fans out as two arrays."""
    tests_yaml = project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text().replace(
            "  - name: extra\n", "  - name: extra\n    resources:\n      mem: 8G\n", 1
        )
    )


def test_a_head_killed_after_its_build_job_still_records_it(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The record opens before the first submission, not after the last, so no window
    has a running job and nothing on disk.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _DyingBackend(die_on_array=1)
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _dispatched_regression()
    assert result.exit_code != 0

    manifest = _run_manifest(minimal_project)
    assert manifest["status"] == "submitting"
    assert manifest["build"]["job_id"] == "fake-build"
    # Exactly what was accepted: the build job, and no array.
    assert manifest["pending"] == []
    assert manifest["submitted_at"] is None
    assert [row["test_name"] for row in manifest["rows"]] == ["basic", "extra"]


def test_a_head_killed_mid_fan_out_records_the_arrays_it_placed(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """One accepted array is a running array, and is recorded as it is accepted."""
    _mark_stub_builder_verilator(minimal_project)
    _split_resource_groups(minimal_project)
    backend = _DyingBackend(die_on_array=2)
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _dispatched_regression()
    assert result.exit_code != 0

    manifest = _run_manifest(minimal_project)
    assert manifest["status"] == "submitting"
    assert manifest["build"]["job_id"] == "fake-build"
    # The first group's element, and only it.
    assert [entry["job_id"] for entry in manifest["pending"]] == ["fake-1"]
    assert [entry["row"] for entry in manifest["pending"]] == [0]


def test_an_incomplete_record_is_an_orphan_to_cancel_but_never_to_adopt(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """`submitting` is live but not collectable.

    Its ids are cancellable because they are real jobs, but not adoptable, because rows
    never submitted would score as "produced no result".
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _DyingBackend(die_on_array=1)
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code != 0
    manifest_path = _run_manifests(minimal_project)[0]
    payload = json.loads(manifest_path.read_text())
    assert payload["status"] == "submitting"
    backend.live = {"fake-build"}

    # adopt: refused, and it says which policy does deal with it.
    result, _rb = _dispatched_regression(["--orphans", "adopt"])
    assert result.exit_code != 0
    message = _fatal_text(result)
    assert "still submitting" in message
    assert "--orphans cancel" in message
    assert json.loads(manifest_path.read_text())["status"] == "submitting"

    # cancel: treated exactly like `running`.
    backend.arrays = 0
    backend.die_on_array = 99
    backend.cancelled_handles = []
    result, _rb = _dispatched_regression(["--orphans", "cancel"])
    assert result.exit_code == 0, result.output
    # Both halves of the compile it had placed, in manifest order.
    assert backend.cancelled_handles == [["fake-verilate", "fake-build"]]
    assert json.loads(manifest_path.read_text())["status"] == "cancelled"


def test_per_run_files_are_named_for_the_run_and_not_just_the_pid(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Every file one head writes for its own jobs carries its run token.

    A pid-reused head would otherwise overwrite the plan and token that an orphan's jobs
    are still reading.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0

    dispatch_root = minimal_project / "artefacts" / ".dispatch"
    plans = list(dispatch_root.glob("plan-*.json"))
    assert len(plans) == 1, [str(path) for path in plans]
    token = json.loads(plans[0].read_text())["run_token"]
    tag = token[:8]
    assert plans[0].name == f"plan-{os.getpid()}-{tag}.json"
    found = list(dispatch_root.glob("run-*.json"))
    assert [path.name for path in found] == [f"run-{os.getpid()}-{tag}.json"]
    # The build envelope, scheduler log and gates map are written by the jobs, so the
    # assertion is on the head's choice of path, which a pid-reused head would collide
    # on.
    build_spec = json.loads(found[0].read_text())["build"]["spec"]
    assert Path(build_spec["result_json"]).name == (
        f"build-result-{os.getpid()}-{tag}.json"
    )
    assert Path(build_spec["log_path"]).name == f"build-{os.getpid()}-{tag}.log"
    assert Path(build_spec["gates_json"]).name == f"gates-{os.getpid()}-{tag}.json"
    assert [path.name for path in backend.array_dirs] == [
        f"array-{os.getpid()}-{tag}-001"
    ]


def test_adopt_refuses_a_fleet_submitted_with_a_different_builder_mode(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Invocation options that never reach the plan (`--builder-mode`, `--builder`,
    `--extra-sim-timeout`, the shared-build root and `--rebuild`) are carried on the
    job specs and compared on adoption.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0
    manifest_path, payload = _orphan_the_run(minimal_project)
    backend.live = set(_all_job_ids(payload))
    submitted_before = list(backend.submitted)
    recorded_mode = payload["build"]["spec"]["builder_mode"]

    result, rb = _invoke(
        [
            "--builder-mode",
            "debug",
            "regression",
            "-c",
            "regression.yaml",
            "-l",
            "5",
            "--dispatch",
            "slurm",
            "--orphans",
            "adopt",
        ]
    )
    rb._artifact_locks.release_all()
    assert result.exit_code != 0
    message = _fatal_text(result)
    assert "builder_mode" in message
    assert repr(recorded_mode) in message
    assert "'debug'" in message
    assert backend.submitted == submitted_before
    assert json.loads(manifest_path.read_text())["status"] == "running"


def test_a_teardown_that_failed_to_cancel_leaves_the_record_running(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The head's own `cancel_all` is best effort, so the record is not marked
    `cancelled` on the strength of having asked.
    """
    _mark_stub_builder_verilator(minimal_project)

    class _FailingWait(_OrphanBackend):
        def wait_all(self, handles, *, extra_wait=0.0):
            raise RuntimeError("controller unreachable")

    backend = _FailingWait(live={"fake-build", "fake-1", "fake-2"}, cancel_works=False)
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    monkeypatch.setattr(RtlBuddy, "ORPHAN_CANCEL_WAIT_S", 0.0)

    result, _rb = _dispatched_regression()
    assert result.exit_code != 0
    assert backend.cancelled
    # The jobs outlived the request, so the record still points at them.
    assert _run_manifest(minimal_project)["status"] == "running"
    assert "after scancel" in _console_text(result)


def test_a_teardown_that_cancelled_cleanly_retires_the_record(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """When the cancellation did take, the record says so and the next run does not
    probe the scheduler.
    """
    _mark_stub_builder_verilator(minimal_project)

    class _FailingWait(_OrphanBackend):
        def wait_all(self, handles, *, extra_wait=0.0):
            raise RuntimeError("controller unreachable")

    backend = _FailingWait(live={"fake-build", "fake-1", "fake-2"})
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _dispatched_regression()
    assert result.exit_code != 0
    assert _run_manifest(minimal_project)["status"] == "cancelled"


def _namespaced_run_manifests(project: Path) -> dict[str, Path]:
    """Each co-located suite's run manifest, keyed by its config filename."""
    found = {}
    for path in sorted(project.glob("artefacts/.dispatch/*/run-*.json")):
        payload = json.loads(path.read_text())
        found[Path(payload["suite_config"]).name] = path
    return found


def test_one_suites_adopt_failure_never_cancels_another_suites_orphan(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A regression adopts per suite, and its teardown is fleet-wide.

    Handles a run inherited are not its to cancel until the fleet-wide wait has begun: a
    later suite's refusal must not cancel a fleet the run adopted but could not collect.
    """
    _mark_stub_builder_verilator(minimal_project)
    _write_colocated_suites(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0

    manifests = _namespaced_run_manifests(minimal_project)
    assert set(manifests) == {"tests.yaml", "other-tests.yaml"}, manifests
    live = set()
    for path in manifests.values():
        payload = json.loads(path.read_text())
        payload["status"] = "running"
        path.write_text(json.dumps(payload))
        live |= set(_all_job_ids(payload))
    backend.live = live
    submitted_before = list(backend.submitted)
    builds_before = list(backend.build_submitted)

    # Break only the second suite's identity, so the first adopts cleanly and the run
    # fails on the second.
    second = manifests["other-tests.yaml"]
    payload = json.loads(second.read_text())
    payload["rows"][0]["test_name"] = "a_test_this_run_does_not_plan"
    second.write_text(json.dumps(payload))

    result, _rb = _dispatched_regression(["--orphans", "adopt"])
    assert result.exit_code != 0
    assert "a_test_this_run_does_not_plan" in _fatal_text(result)
    # No handle was cancelled, above all not the fleet the first suite adopted, and
    # nothing was submitted.
    assert [ids for ids in backend.cancelled_handles if ids] == []
    assert backend.submitted == submitted_before
    assert backend.build_submitted == builds_before
    # Both records still point at live fleets, so the run can be retried.
    for path in manifests.values():
        assert json.loads(path.read_text())["status"] == "running"


def _set_dispatch_resources(project: Path, **fields):
    """Give this project a `cfg-dispatch.resources` block."""
    root_cfg = project / "root_config.yaml"
    body = "".join(f"    {key}: {value}\n" for key, value in fields.items())
    root_cfg.write_text(root_cfg.read_text() + "\ncfg-dispatch:\n  resources:\n" + body)


def test_adopt_refuses_a_fleet_reserved_under_a_different_cfg_dispatch(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The resolved reservation is per-run configuration, not plan.

    A test inheriting `cfg-dispatch.resources` carries no reservation in the plan, so
    raising `time` changes nothing the plan comparison sees. Adoption must be refused,
    or the old limit's TIMEOUT becomes this run's verdict.
    """
    _mark_stub_builder_verilator(minimal_project)
    _set_dispatch_resources(minimal_project, cpus=2, time='"00:30:00"')
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0
    manifest_path, payload = _orphan_the_run(minimal_project)
    backend.live = set(_all_job_ids(payload))
    submitted_before = list(backend.submitted)
    assert payload["pending"][0]["spec"]["resources"]["time"] == "00:30:00"

    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(root_cfg.read_text().replace('"00:30:00"', '"02:00:00"'))

    result, _rb = _dispatched_regression(["--orphans", "adopt"])
    assert result.exit_code != 0
    message = _fatal_text(result)
    assert "resources.time" in message
    assert "'00:30:00'" in message
    assert "'02:00:00'" in message
    assert backend.submitted == submitted_before
    assert json.loads(manifest_path.read_text())["status"] == "running"


def test_adopt_accepts_a_fleet_whose_reservation_is_unchanged(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The same config still adopts, so the check is a comparison, not a blanket
    refusal.
    """
    _mark_stub_builder_verilator(minimal_project)
    _set_dispatch_resources(minimal_project, cpus=2, time='"00:30:00"')
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0
    manifest_path, payload = _orphan_the_run(minimal_project)
    backend.live = set(_all_job_ids(payload))
    submitted_before = list(backend.submitted)

    result, _rb = _dispatched_regression(["--orphans", "adopt"])
    assert result.exit_code == 0, result.output
    assert backend.submitted == submitted_before
    assert json.loads(manifest_path.read_text())["status"] == "collected"


def test_adopt_reads_the_shared_build_root_the_jobs_were_given(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """An empty string (explicit disable) and `None` (nothing asked) are different
    instructions to a job; the check compares what the specs carry, not the head's
    resolved root.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )

    def _regression(*extra):
        result, rb = _invoke(
            [
                "regression",
                "-c",
                "regression.yaml",
                "-l",
                "5",
                "--dispatch",
                "slurm",
                *extra,
            ]
        )
        rb._artifact_locks.release_all()
        return result

    # An orphan submitted with the cache explicitly OFF.
    assert _regression("--shared-build-root", "").exit_code == 0
    manifest_path, payload = _orphan_the_run(minimal_project)
    backend.live = set(_all_job_ids(payload))
    # That is what the jobs were told, and `None` would have said nothing.
    assert payload["build"]["spec"]["shared_build_root"] == ""

    # Saying nothing is NOT the same instruction, so it is refused.
    result = _regression("--orphans", "adopt")
    assert result.exit_code != 0
    message = _fatal_text(result)
    assert "shared_build_root" in message
    assert json.loads(manifest_path.read_text())["status"] == "running"

    # Repeating the same disable is, so it adopts.
    result = _regression("--shared-build-root", "", "--orphans", "adopt")
    assert result.exit_code == 0, result.output
    assert json.loads(manifest_path.read_text())["status"] == "collected"


def test_a_plain_test_run_finds_a_namespaced_regressions_orphan(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Discovery searches the suite's `.dispatch/` tree, not one directory: a regression
    namespaces each suite's files below it, while a plain `rb test` writes to
    `.dispatch/` itself. The suite config recorded in each manifest keeps the scan to
    this suite's records.
    """
    _mark_stub_builder_verilator(minimal_project)
    _write_colocated_suites(minimal_project)
    backend = _OrphanBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    assert _dispatched_regression()[0].exit_code == 0

    manifests = _namespaced_run_manifests(minimal_project)
    assert set(manifests) == {"tests.yaml", "other-tests.yaml"}, manifests
    mine, theirs = manifests["tests.yaml"], manifests["other-tests.yaml"]
    for path in manifests.values():
        payload = json.loads(path.read_text())
        payload["status"] = "running"
        path.write_text(json.dumps(payload))
    ours = json.loads(mine.read_text())
    backend.live = set(_all_job_ids(ours))

    result, rb = _invoke(
        ["test", "-c", "tests.yaml", "--dispatch", "slurm", "--orphans", "cancel"]
    )
    rb._artifact_locks.release_all()
    assert result.exit_code == 0, result.output
    # Found from the namespaced directory `rb test` does not write to...
    assert len(backend.cancelled_handles) >= 1
    assert sorted(backend.cancelled_handles[0]) == sorted(_all_job_ids(ours))
    assert json.loads(mine.read_text())["status"] == "cancelled"
    assert json.loads(theirs.read_text())["status"] == "running"


def test_the_cancel_probe_is_bounded_by_the_grace_it_has_left(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Each re-probe carries a deadline, so a wedged controller cannot hold the grace
    period open past its length.
    """
    _mark_stub_builder_verilator(minimal_project)
    backend = _OrphanBackend(cancel_works=False)
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    monkeypatch.setattr(RtlBuddy, "ORPHAN_CANCEL_WAIT_S", 0.0)
    assert _dispatched_regression()[0].exit_code == 0
    _manifest_path, payload = _orphan_the_run(minimal_project)
    backend.live = set(_all_job_ids(payload))
    backend.probe_timeouts = []

    result, _rb = _dispatched_regression(["--orphans", "cancel"])
    assert result.exit_code != 0
    # Discovery asks without a deadline; the cancellation check always uses one, at most
    # one poll interval when the grace is spent.
    assert backend.probe_timeouts[0] is None
    assert backend.probe_timeouts[-1] == RtlBuddy.ORPHAN_CANCEL_POLL_S


class _SplittingBackend(_RecordingBackend):
    """A recording fake that claims the Slurm backend name; the head only splits a
    compile for Slurm.
    """

    name = "slurm"


def _splitting_run(monkeypatch, project, argv=(), **kwargs):
    _mark_stub_builder_verilator(project)
    backend = _SplittingBackend(**kwargs)
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _invoke(
        [
            "--machine",
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "slurm",
            *argv,
        ]
    )
    return backend, result


def test_a_verilate_job_is_chained_in_front_of_the_build_job(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Two reservations in order, and the fan-out still waits on the build job alone."""
    backend, result = _splitting_run(monkeypatch, minimal_project)
    assert result.exit_code == 0, result.output

    assert [spec.phase for spec in backend.verilate_submitted] == ["verilate"]
    assert [spec.phase for spec in backend.build_submitted] == ["build"]
    # The verilate job goes out first, ungated; the build job waits on it.
    assert backend.build_dependencies == [None, "fake-verilate"]
    # Sims keep their `afterok` on the build job only: a sim released by the verilation
    # would find no executable.
    assert {call["dependency"] for call in backend.array_calls} == {"fake-build"}


def test_the_verilate_job_owns_its_own_envelope_log_and_gate(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Split-compile jobs mirror the build job's naming so the halves never overwrite
    each other, and carry no gates manifest.
    """
    backend, result = _splitting_run(monkeypatch, minimal_project)
    assert result.exit_code == 0, result.output

    (verilate,) = backend.verilate_submitted
    (build,) = backend.build_submitted
    assert Path(verilate.result_json).name.startswith("verilate-result-")
    assert Path(verilate.log_path).name.startswith("verilate-")
    assert verilate.gates_json is None
    # The build job's own names are untouched.
    assert Path(build.result_json).name.startswith("build-result-")
    assert build.gates_json is not None


def test_a_non_verilator_suite_is_not_split(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The split rewrites the Verilator front end off the compile line, so it is offered
    only where that line is Verilator's.
    """
    _set_stub_builder_family(minimal_project, "vcs")
    backend = _SplittingBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", _backend_factory(backend)
    )
    result, _rb = _invoke(
        ["--machine", "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    assert backend.verilate_submitted == []
    assert [spec.phase for spec in backend.build_submitted] == ["full"]
    assert backend.build_dependencies == [None]


def test_split_verilate_false_keeps_the_single_build_job(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The escape hatch: one job, with an unchanged argv."""
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n  compile:\n    cpus: 4\n    split-verilate: false\n",
    )
    backend, result = _splitting_run(monkeypatch, minimal_project)
    assert result.exit_code == 0, result.output

    assert backend.verilate_submitted == []
    assert [spec.phase for spec in backend.build_submitted] == ["full"]


def test_a_suite_may_turn_the_split_off_for_itself(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        "compile:\n  split-verilate: false\n" + tests_yaml.read_text()
    )
    backend, result = _splitting_run(monkeypatch, minimal_project)
    assert result.exit_code == 0, result.output
    assert backend.verilate_submitted == []


def test_the_verilate_job_is_reserved_from_its_own_block(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """`compile.cpus` sizes the make; the verilation is single-threaded and takes
    `compile.verilate`, with mem and time inherited where unstated.
    """
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "01:00:00"\n'
        '  compile:\n    cpus: 4\n    mem: 8G\n    time: "02:00:00"\n'
        "    verilate:\n      mem: 32G\n",
    )
    backend, result = _splitting_run(monkeypatch, minimal_project)
    assert result.exit_code == 0, result.output

    (verilate,) = backend.verilate_submitted
    (build,) = backend.build_submitted
    assert (verilate.resources.cpus, verilate.resources.mem) == (2, "32G")
    assert verilate.resources.time == "02:00:00"
    assert (build.resources.cpus, build.resources.mem) == (4, "8G")


def test_collect_attaches_telemetry_to_both_halves_of_the_compile(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """Two jobs, two sacct rows, two envelopes, and one telemetry query for both."""
    backend, result = _splitting_run(
        monkeypatch,
        minimal_project,
        telemetry={
            "fake-1": {"state": "COMPLETED", "elapsed_s": 5, "timelimit_s": 3600},
            "fake-build": {"state": "COMPLETED", "elapsed_s": 30, "timelimit_s": 7200},
            "fake-verilate": {
                "state": "COMPLETED",
                "elapsed_s": 70,
                "timelimit_s": 7200,
            },
        },
        build_result={"built": ["basic"], "failed": [], "builds": []},
    )
    assert result.exit_code == 0, result.output

    assert backend.telemetry_queries[0][:2] == ["fake-verilate", "fake-build"]
    (verilate,) = backend.verilate_submitted
    envelope = json.loads(Path(verilate.result_json).read_text())
    assert envelope["telemetry"]["elapsed_s"] == 70


def test_both_halves_of_the_compile_get_a_reservation_advice_row(
    minimal_project: Path,
    stub_build_runner: type[_StubBuildRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """Each job's wall clock is its own to size, so each gets its own row."""
    _add_dispatch_resources(
        minimal_project,
        "\ncfg-dispatch:\n"
        '  resources:\n    cpus: 1\n    mem: 2G\n    time: "01:00:00"\n'
        '  compile:\n    cpus: 4\n    mem: 8G\n    time: "02:00:00"\n',
    )
    compiled = [
        {
            "test": "basic",
            "builder": "hook-chosen-builder",
            "duration_sec": 42.5,
            "reused": False,
            "group": "obj_dir_cafe",
        }
    ]
    backend, result = _splitting_run(
        monkeypatch,
        minimal_project,
        telemetry={
            "fake-1": {"state": "COMPLETED", "elapsed_s": 5, "timelimit_s": 3600},
            "fake-build": {"state": "COMPLETED", "elapsed_s": 60, "timelimit_s": 7200},
            "fake-verilate": {
                "state": "COMPLETED",
                "elapsed_s": 60,
                "timelimit_s": 7200,
            },
        },
        build_result={"built": ["basic"], "failed": [], "builds": compiled},
    )
    assert result.exit_code == 0, result.output

    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    advice = json.loads(payload_line)["payload"]["reservation_advice"]
    rows = {entry["phase"]: entry for entry in advice if entry["resource"] == "time"}
    assert rows["compile"]["test"] == "(build job)"
    assert rows["compile"]["edit_hint"]["path"] == "cfg-dispatch.compile.time"
    assert rows["verilate"]["test"] == "(verilate job)"
    assert rows["verilate"]["edit_hint"]["path"] == "cfg-dispatch.compile.verilate.time"


def test_a_cancelled_fan_out_names_the_verilate_jobs_log(
    minimal_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A fan-out reaped by `kill-on-invalid-dep` never had a build job run, so the log
    that says why is the verilate job's.
    """
    backend, result = _splitting_run(monkeypatch, minimal_project, write_results=False)
    assert result.exit_code != 0

    (verilate,) = backend.verilate_submitted
    assert str(verilate.log_path) in result.output.replace("\n", "")


def test_a_plusarg_override_reaches_the_plan_and_every_sim_job(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    """`rb test --plusarg` survives dispatch.

    The merged plusargs are in the plan the jobs rebuild their configs from, and the
    spec carries them too, so the job records the overrides in its envelope.
    """
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(
        [
            "test",
            "basic",
            "-c",
            "tests.yaml",
            "--dispatch",
            "slurm",
            "--plusarg",
            "mutate=1",
        ]
    )
    assert result.exit_code == 0, result.output

    spec = fake_backend.submitted[0]
    assert spec.plusarg_overrides == {"mutate": "1"}
    planned = read_plan_config(spec.plan_path, "basic")
    assert planned.get_plusargs() == {"mutate": "1"}
    # The build job compiles the plan's configs, so its PRE hook sees the same merged
    # view.
    assert fake_backend.build_submitted[0].plan_path == spec.plan_path


def test_a_dispatched_run_without_the_flag_plans_no_overrides(
    minimal_project: Path,
    fake_backend: _FakeBackend,
):
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(["test", "basic", "-c", "tests.yaml", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    assert fake_backend.submitted[0].plusarg_overrides == {}
    assert (
        read_plan_config(fake_backend.submitted[0].plan_path, "basic").get_plusargs()
        is None
    )


def _write_mode_reservations(project: Path):
    """Size this suite's sim and compile reservations per builder mode."""
    tests_yaml = project / "tests.yaml"
    tests_yaml.write_text(
        "compile:\n"
        "  mem: 8G\n"
        "  modes:\n"
        "    cov: {mem: 96G}\n"
        + tests_yaml.read_text().replace(
            "  - name: tb_basic\n",
            "  - name: tb_basic\n"
            "    resources:\n"
            "      mem: 1G\n"
            '      time: "00:15:00"\n'
            "      modes:\n"
            '        cov: {mem: 16G, time: "00:30:00"}\n',
        )
    )


@pytest.mark.parametrize(
    "mode,sim_mem,sim_time,build_mem",
    [
        # `-M reg` is `rb regression`'s default, so an invocation with no -M reserves
        # the same base figures.
        ("reg", "1G", "00:15:00", "8G"),
        # A coverage run reserves what the `modes.cov` blocks ask for, on the sim jobs
        # and the build job.
        ("cov", "16G", "00:30:00", "96G"),
    ],
)
def test_the_dispatched_plan_reserves_the_modes_figures(
    minimal_project: Path,
    fake_backend: _FakeBackend,
    mode,
    sim_mem,
    sim_time,
    build_mem,
):
    _mark_stub_builder_verilator(minimal_project)
    _write_mode_reservations(minimal_project)
    # `-M` is a global option, so it precedes the subcommand.
    result, _ = _invoke(
        ["-M", mode, "regression", "-c", "regression.yaml", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    sim = fake_backend.submitted[0]
    # The mode and the reservation resolved for it come from the same value, so a
    # coverage build never runs in a regression-sized allocation.
    assert sim.builder_mode == mode
    assert (sim.resources.mem, sim.resources.time) == (sim_mem, sim_time)
    assert fake_backend.build_submitted[0].resources.mem == build_mem


def test_an_invalid_job_tag_fails_the_run_before_anything_is_submitted(
    minimal_project: Path, monkeypatch: pytest.MonkeyPatch
):
    """`RTL_BUDDY_JOB_TAG` is validated when the Slurm backend is built (#693)."""
    import subprocess

    from rtl_buddy.dispatch import slurm as slurm_module

    calls = []
    real_run = subprocess.run

    def run(argv, *args, **kwargs):
        if argv[0] in ("sbatch", "squeue", "scontrol"):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "not a real cluster")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(slurm_module, "require_tool", lambda name: None)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("RTL_BUDDY_JOB_TAG", "ci,run")
    _mark_stub_builder_verilator(minimal_project)

    result, _ = _invoke(["regression", "-c", "regression.yaml", "--dispatch", "slurm"])

    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "RTL_BUDDY_JOB_TAG='ci,run'" in str(result.exception)
    assert calls == []
