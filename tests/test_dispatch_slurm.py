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

    def run(argv, capture_output=True, text=True, cwd=None, timeout=None):
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
        DispatchConfigFile(sbatch_args=["--partition=verif"]).initialise()
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
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

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
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    with pytest.raises(FatalRtlBuddyError, match="no partition"):
        backend.submit(_spec())


def test_wait_all_polls_until_queue_drains(monkeypatch):
    calls = []
    results = [
        SimpleNamespace(returncode=0, stdout="1|Resources\n2|Priority\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="2|None\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="", stderr=""),
    ]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    monkeypatch.setattr(slurm_module.time, "sleep", lambda s: None)
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    handles = [JobHandle("1", _spec()), JobHandle("2", _spec(run_id=1))]
    backend.wait_all(handles)

    assert len(calls) == 3
    assert calls[0][0] == "squeue"
    assert "--jobs" in calls[0] and "1,2" in calls[0]


def test_wait_all_cancels_jobs_whose_dependency_can_never_be_satisfied(monkeypatch):
    """A failed build leaves its afterok dependents PENDING forever (#358).

    Slurm only reaps them when the site sets `kill_invalid_depend`, which is
    off by default — so PD would keep the head polling until it is killed.
    Cancel them and stop waiting; collection reports them as no-result.
    """
    calls, results = (
        [],
        [
            SimpleNamespace(
                returncode=0,
                stdout="7_[1-3]|DependencyNeverSatisfied\n9|Resources\n",
                stderr="",
            ),
            SimpleNamespace(returncode=0, stdout="", stderr=""),  # scancel
            SimpleNamespace(returncode=0, stdout="", stderr=""),  # queue drained
        ],
    )
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    monkeypatch.setattr(slurm_module.time, "sleep", lambda s: None)
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.wait_all([JobHandle("7_1", _spec()), JobHandle("9", _spec(run_id=1))])

    # The doomed array was cancelled by BASE id (one scancel clears it all),
    # and the still-queued job 9 kept the wait going for one more poll.
    scancels = [argv for argv in calls if argv[0] == "scancel"]
    assert scancels == [["scancel", "7"]]
    assert len([argv for argv in calls if argv[0] == "squeue"]) == 2


def test_wait_all_returns_when_only_doomed_jobs_remain(monkeypatch):
    """Nothing else queued: the head must not poll a second time."""
    calls, results = (
        [],
        [
            SimpleNamespace(
                returncode=0, stdout="7|DependencyNeverSatisfied\n", stderr=""
            ),
            SimpleNamespace(returncode=0, stdout="", stderr=""),  # scancel
        ],
    )
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    monkeypatch.setattr(slurm_module.time, "sleep", lambda s: None)
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.wait_all([JobHandle("7", _spec())])

    assert [argv[0] for argv in calls] == ["squeue", "scancel"]


def test_wait_all_asks_squeue_for_the_reason_field(monkeypatch):
    calls, results = ([], [SimpleNamespace(returncode=0, stdout="", stderr="")])
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    SlurmDispatchBackend(DispatchConfigFile().initialise()).wait_all(
        [JobHandle("1", _spec())]
    )
    assert "--format=%i|%r" in calls[0]


def test_wait_all_treats_squeue_error_as_drained(monkeypatch):
    # Once every job has aged out of the queue, squeue exits nonzero with
    # "Invalid job id specified" — that is completion, not failure.
    calls, results = (
        [],
        [SimpleNamespace(returncode=1, stdout="", stderr="Invalid job id specified")],
    )
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.wait_all([JobHandle("99", _spec())])
    assert len(calls) == 1


def test_wait_all_no_handles_is_a_no_op(monkeypatch):
    calls = []
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, []))
    SlurmDispatchBackend(DispatchConfigFile().initialise()).wait_all([])
    assert calls == []


def test_cancel_all_scancels_every_job(monkeypatch):
    calls, results = [], []
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.cancel_all([JobHandle("5", _spec()), JobHandle("6", _spec(run_id=2))])
    (argv,) = calls
    assert argv == ["scancel", "5", "6"]


def test_cancel_all_ignores_none_handles(monkeypatch):
    # cancel_all is the last line of defence against an orphaned fleet, so a
    # None handle (e.g. a zero-test suite's absent build handle, #361) must
    # not disarm it — it still scancels the real jobs.
    calls, results = [], []
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.cancel_all([None, JobHandle("7", _spec()), None])
    (argv,) = calls
    assert argv == ["scancel", "7"]


# ---------------------------------------------------------------- P2: arrays


def test_submit_array_builds_manifest_script_and_throttle(monkeypatch, tmp_path):
    calls, results = [], [SimpleNamespace(returncode=0, stdout="500\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile(sbatch_args=["--qos=fast"]))

    specs = [_spec(run_id=i) for i in (1, 2, 3)]
    array_dir = tmp_path / "array-001"
    handles = backend.submit_array(specs, array_dir=array_dir, max_parallel=2)

    assert [h.job_id for h in handles] == ["500_1", "500_2", "500_3"]
    manifest = (array_dir / "manifest.txt").read_text().splitlines()
    assert len(manifest) == 3
    assert "--run-id 1" in manifest[0] and "--run-id 3" in manifest[2]
    assert (array_dir / "array.sh").read_text().startswith("#!/bin/bash")
    # Element logs are deterministic and stamped back onto the specs.
    assert specs[0].log_path == array_dir / "slurm-1.log"

    (argv,) = calls
    assert "--array=1-3%2" in argv
    assert f"--output={array_dir}/slurm-%a.log" in argv
    assert "--qos=fast" in argv
    assert argv[-2:] == [str(array_dir / "array.sh"), str(array_dir / "manifest.txt")]


def test_submit_array_no_throttle_when_cap_exceeds_size(monkeypatch, tmp_path):
    calls, results = [], [SimpleNamespace(returncode=0, stdout="7\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile())

    backend.submit_array(
        [_spec(run_id=1), _spec(run_id=2)], array_dir=tmp_path, max_parallel=200
    )
    (argv,) = calls
    assert "--array=1-2" in argv


def test_submit_array_single_spec_falls_back_to_submit(monkeypatch, tmp_path):
    calls, results = [], [SimpleNamespace(returncode=0, stdout="9\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile())

    handles = backend.submit_array([_spec()], array_dir=tmp_path, max_parallel=8)
    assert [h.job_id for h in handles] == ["9"]
    (argv,) = calls
    assert not any(a.startswith("--array") for a in argv)
    assert "--wrap" in argv


def test_wait_and_cancel_use_base_array_ids(monkeypatch):
    calls, results = [], [SimpleNamespace(returncode=0, stdout="", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile(poll_interval=0.0))

    handles = [
        JobHandle("500_1", _spec(run_id=1)),
        JobHandle("500_2", _spec(run_id=2)),
        JobHandle("42", _spec()),
    ]
    backend.wait_all(handles)
    assert "500,42" in calls[0]

    backend.cancel_all(handles)
    assert calls[1] == ["scancel", "500", "42"]


# ------------------------------------------------------- P2: sacct telemetry


def test_collect_telemetry_parses_allocation_and_step_rows(monkeypatch):
    sacct_out = "\n".join(
        [
            # JobID|State|ElapsedRaw|TimelimitRaw|AllocCPUS|ReqMem|TotalCPU|MaxRSS
            "500_1|COMPLETED|75|60|2|4G||",
            "500_1.batch|COMPLETED|75||2||01:02.500|2948K",
            "500_2|TIMEOUT|3600|60|2|4G||",
            "500_2.batch|CANCELLED|3600||2||59:00.000|1.5G",
            "42|COMPLETED|10|1|1|500M||",
            "42.batch|COMPLETED|10||1||00:03.250|10240K",
        ]
    )
    calls, results = [], [SimpleNamespace(returncode=0, stdout=sacct_out, stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile())

    handles = [
        JobHandle("500_1", _spec(run_id=1)),
        JobHandle("500_2", _spec(run_id=2)),
        JobHandle("42", _spec()),
    ]
    telemetry = backend.collect_telemetry(handles)

    (argv,) = calls
    assert argv[0] == "sacct" and "500,42" in argv

    t1 = telemetry["500_1"]
    assert t1["state"] == "COMPLETED"
    assert t1["elapsed_s"] == 75
    assert t1["timelimit_s"] == 3600  # TimelimitRaw is minutes
    assert t1["alloc_cpus"] == 2
    assert t1["req_mem_bytes"] == 4 * 2**30
    assert t1["max_rss_bytes"] == 2948 * 1024
    assert t1["total_cpu_s"] == 62.5

    t2 = telemetry["500_2"]
    assert t2["state"] == "TIMEOUT"
    assert t2["max_rss_bytes"] == int(1.5 * 2**30)

    assert telemetry["42"]["total_cpu_s"] == 3.25


def test_collect_telemetry_no_accounting_degrades_to_empty(monkeypatch):
    calls, results = (
        [],
        [
            SimpleNamespace(
                returncode=1, stdout="", stderr="sacct: error: accounting disabled"
            )
        ],
    )
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile())
    assert backend.collect_telemetry([JobHandle("5", _spec())]) == {}


def test_mem_and_cpu_time_parsers():
    assert slurm_module._parse_mem_to_bytes("2948K") == 2948 * 1024
    assert slurm_module._parse_mem_to_bytes("4Gn") == 4 * 2**30
    assert slurm_module._parse_mem_to_bytes("1.5G") == int(1.5 * 2**30)
    assert slurm_module._parse_mem_to_bytes("123") == 123
    assert slurm_module._parse_mem_to_bytes("") is None
    assert slurm_module._parse_cpu_time_to_seconds("01:02.500") == 62.5
    assert slurm_module._parse_cpu_time_to_seconds("2-01:00:00") == 2 * 86400 + 3600
    assert slurm_module._parse_cpu_time_to_seconds("") is None


# ------------------------------------------- P2 review: telemetry robustness


def test_collect_telemetry_sums_cpu_time_across_steps(monkeypatch):
    # TotalCPU is per step; a job's CPU time is the SUM (.batch + srun step),
    # while MaxRSS stays a high-water max.
    sacct_out = "\n".join(
        [
            "9|COMPLETED|100|60|4|4G||",
            "9.batch|COMPLETED|100||4||00:10.000|500M",
            "9.0|COMPLETED|100||4||01:30.000|900M",
        ]
    )
    calls, results = [], [SimpleNamespace(returncode=0, stdout=sacct_out, stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile())

    t = backend.collect_telemetry([JobHandle("9", _spec())])["9"]
    assert t["total_cpu_s"] == 100.0  # 10s + 90s summed
    assert t["max_rss_bytes"] == 900 * 2**20  # max, not sum


def test_collect_telemetry_missing_sacct_binary_degrades(monkeypatch):
    # sacct absent (FileNotFoundError) must not fail a finished run.
    def boom(*a, **k):
        raise FileNotFoundError("sacct")

    monkeypatch.setattr(slurm_module.subprocess, "run", boom)
    backend = SlurmDispatchBackend(DispatchConfigFile())
    assert backend.collect_telemetry([JobHandle("1", _spec())]) == {}


def test_collect_telemetry_timeout_degrades(monkeypatch):
    def slow(*a, **k):
        raise slurm_module.subprocess.TimeoutExpired(cmd="sacct", timeout=60)

    monkeypatch.setattr(slurm_module.subprocess, "run", slow)
    backend = SlurmDispatchBackend(DispatchConfigFile())
    assert backend.collect_telemetry([JobHandle("1", _spec())]) == {}


def test_array_script_fails_loud_on_missing_manifest_line():
    # The array runner exits non-zero (not a silent COMPLETED) when the
    # SLURM_ARRAY_TASK_ID line is absent.
    assert "set -uo pipefail" in slurm_module._ARRAY_SCRIPT
    assert "exit 2" in slurm_module._ARRAY_SCRIPT


# ------------------------------------------ dispatched build job + dependency


def test_submit_build_builds_argv(monkeypatch):
    from rtl_buddy.dispatch.base import BuildJobSpec

    calls, results = [], [SimpleNamespace(returncode=0, stdout="900\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(
        DispatchConfigFile(sbatch_args=["--partition=verif"]).initialise()
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
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.submit(_spec(), dependency="900")
    (argv,) = calls
    assert "--dependency=afterok:900" in argv


def test_submit_sim_without_dependency_has_no_flag(monkeypatch):
    calls, results = [], [SimpleNamespace(returncode=0, stdout="12\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.submit(_spec())
    (argv,) = calls
    assert not any(a.startswith("--dependency") for a in argv)


def test_build_argv_carries_plan_and_result_json(monkeypatch):
    from rtl_buddy.dispatch.base import BuildJobSpec

    calls, results = [], [SimpleNamespace(returncode=0, stdout="900\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    spec = BuildJobSpec(
        suite_dir="/proj/verif/blk",
        test_config_path="/proj/verif/blk/tests.yaml",
        resources=JobResources(cpus=8, mem="16G", time="02:00:00"),
        plan_path=Path("/proj/verif/blk/artefacts/.dispatch/plan-1.json"),
        result_json=Path("/proj/verif/blk/artefacts/.dispatch/build-result-1.json"),
    )
    backend.submit_build(spec)
    (argv,) = calls
    wrapped = shlex.split(argv[argv.index("--wrap") + 1])
    assert wrapped[wrapped.index("--plan") + 1] == str(spec.plan_path)
    assert wrapped[wrapped.index("--result-json") + 1] == str(spec.result_json)


def test_sim_argv_carries_plan(monkeypatch):
    calls, results = [], [SimpleNamespace(returncode=0, stdout="12\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    plan = Path("/proj/verif/blk/artefacts/.dispatch/plan-1.json")
    backend.submit(_spec(plan_path=plan))
    (argv,) = calls
    wrapped = shlex.split(argv[argv.index("--wrap") + 1])
    assert wrapped[wrapped.index("--plan") + 1] == str(plan)


def test_submit_array_accepts_dependency(monkeypatch, tmp_path):
    calls, results = [], [SimpleNamespace(returncode=0, stdout="500\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.submit_array(
        [_spec(run_id=1), _spec(run_id=2)],
        array_dir=tmp_path / "array-001",
        max_parallel=4,
        dependency="900",
    )
    (argv,) = calls
    assert "--dependency=afterok:900" in argv
