"""Slurm dispatch backend unit tests (#351 P1): sbatch argv shape,
job-id capture, queue-drain polling, and cancellation — all against a
faked subprocess layer, no Slurm required."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from rtl_buddy.config.dispatch import DispatchConfigFile, JobResources
from rtl_buddy.dispatch.base import JobHandle, TestJobSpec
from rtl_buddy.dispatch.slurm import SlurmDispatchBackend
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.seed_mode import SeedMode

from rtl_buddy.dispatch import slurm as slurm_module


@pytest.fixture(autouse=True)
def _no_tool_check(monkeypatch):
    # SlurmDispatchBackend.__init__ asserts the Slurm client is installed;
    # these unit tests construct it directly with no sbatch on PATH.
    monkeypatch.setattr(slurm_module, "require_tool", lambda name: None)


def _spec(**overrides) -> TestJobSpec:
    defaults = dict(
        test_name="basic",
        suite_dir="/proj/verif/blk",
        test_config_path="/proj/verif/blk/tests.yaml",
        result_json=Path("/proj/verif/blk/artefacts/basic/dispatch/result.json"),
        resources=JobResources(cpus=2, mem=None, time="01:00:00"),
    )
    defaults.update(overrides)
    return TestJobSpec(**defaults)


def _fake_run(calls, results):
    """subprocess.run stand-in: records argv, pops canned results."""

    def run(argv, capture_output=True, text=True, cwd=None):
        calls.append(list(argv))
        result = (
            results.pop(0)
            if results
            else SimpleNamespace(returncode=0, stdout="", stderr="")
        )
        return result

    return run


def test_submit_builds_sbatch_argv_and_parses_job_id(monkeypatch):
    calls, results = (
        [],
        [SimpleNamespace(returncode=0, stdout="123;cluster\n", stderr="")],
    )
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(
        DispatchConfigFile(sbatch_args=["--partition=verif"])
    )

    handle = backend.submit(_spec(log_path=Path("/tmp/slurm.log")))

    assert handle.job_id == "123"
    (argv,) = calls
    assert argv[0] == "sbatch"
    assert "--parsable" in argv
    assert "--job-name=rb:basic" in argv
    assert "--chdir=/proj/verif/blk" in argv
    assert "--time=01:00:00" in argv  # always explicit
    assert "--cpus-per-task=2" in argv
    assert not any(a.startswith("--mem") for a in argv)  # mem unset → no flag
    assert "--output=/tmp/slurm.log" in argv
    assert "--partition=verif" in argv  # sbatch-args passthrough

    wrapped = shlex.split(argv[argv.index("--wrap") + 1])
    assert wrapped[0] == sys.executable
    assert wrapped[1:4] == ["-m", "rtl_buddy", "--machine"]
    assert "_test-job" in wrapped
    assert "basic" in wrapped
    assert "--share-build" in wrapped
    assert "--result-json" in wrapped


def test_submit_mem_and_run_id_and_seed_flags(monkeypatch):
    calls, results = [], [SimpleNamespace(returncode=0, stdout="7\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile())

    spec = _spec(
        resources=JobResources(cpus=1, mem="24G", time="04:00:00"),
        run_id=3,
        seed_mode=SeedMode.NEW,
    )
    handle = backend.submit(spec)

    assert handle.job_id == "7"
    (argv,) = calls
    assert "--mem=24G" in argv
    assert "--job-name=rb:basic:3" in argv
    wrapped = shlex.split(argv[argv.index("--wrap") + 1])
    assert wrapped[wrapped.index("--run-id") + 1] == "3"
    assert wrapped[wrapped.index("--seed-mode") + 1] == "new"


def test_submit_failure_fails_loud(monkeypatch):
    calls, results = (
        [],
        [
            SimpleNamespace(
                returncode=1, stdout="", stderr="sbatch: error: no partition"
            )
        ],
    )
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile())

    with pytest.raises(FatalRtlBuddyError, match="no partition"):
        backend.submit(_spec())


def test_wait_all_polls_until_queue_drains(monkeypatch):
    calls = []
    results = [
        SimpleNamespace(returncode=0, stdout="1\n2\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="2\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="", stderr=""),
    ]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    monkeypatch.setattr(slurm_module.time, "sleep", lambda s: None)
    backend = SlurmDispatchBackend(DispatchConfigFile(poll_interval=0.0))

    handles = [JobHandle("1", _spec()), JobHandle("2", _spec(run_id=1))]
    backend.wait_all(handles)

    assert len(calls) == 3
    assert calls[0][0] == "squeue"
    assert "--jobs" in calls[0] and "1,2" in calls[0]


def test_wait_all_treats_squeue_error_as_drained(monkeypatch):
    # Once every job has aged out of the queue, squeue exits nonzero with
    # "Invalid job id specified" — that is completion, not failure.
    calls, results = (
        [],
        [SimpleNamespace(returncode=1, stdout="", stderr="Invalid job id specified")],
    )
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile(poll_interval=0.0))

    backend.wait_all([JobHandle("99", _spec())])
    assert len(calls) == 1


def test_wait_all_no_handles_is_a_no_op(monkeypatch):
    calls = []
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, []))
    SlurmDispatchBackend(DispatchConfigFile()).wait_all([])
    assert calls == []


def test_cancel_all_scancels_every_job(monkeypatch):
    calls, results = [], []
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile())

    backend.cancel_all([JobHandle("5", _spec()), JobHandle("6", _spec(run_id=2))])
    (argv,) = calls
    assert argv == ["scancel", "5", "6"]


# ------------------------------------------ dispatched build job + dependency


def test_submit_build_builds_argv(monkeypatch):
    from rtl_buddy.dispatch.base import BuildJobSpec

    calls, results = [], [SimpleNamespace(returncode=0, stdout="900\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(
        DispatchConfigFile(sbatch_args=["--partition=verif"])
    )

    spec = BuildJobSpec(
        suite_dir="/proj/verif/blk",
        test_config_path="/proj/verif/blk/tests.yaml",
        resources=JobResources(cpus=8, mem="16G", time="02:00:00"),
        reg_level=1000,
        log_path=Path("/proj/verif/blk/artefacts/.dispatch/build.log"),
    )
    handle = backend.submit_build(spec)

    assert handle.job_id == "900"
    (argv,) = calls
    assert argv[0] == "sbatch"
    assert "--job-name=rb-build" in argv
    assert "--time=02:00:00" in argv and "--cpus-per-task=8" in argv
    assert "--mem=16G" in argv
    assert "--partition=verif" in argv
    wrapped = shlex.split(argv[argv.index("--wrap") + 1])
    assert "_build-job" in wrapped
    assert "--share-build" in wrapped
    assert wrapped[wrapped.index("-l") + 1] == "1000"
    assert "_test-job" not in wrapped  # it's a build, not a sim


def test_submit_sim_with_dependency_adds_afterok(monkeypatch):
    calls, results = [], [SimpleNamespace(returncode=0, stdout="12\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile())

    backend.submit(_spec(), dependency="900")
    (argv,) = calls
    assert "--dependency=afterok:900" in argv


def test_submit_sim_without_dependency_has_no_flag(monkeypatch):
    calls, results = [], [SimpleNamespace(returncode=0, stdout="12\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile())

    backend.submit(_spec())
    (argv,) = calls
    assert not any(a.startswith("--dependency") for a in argv)
