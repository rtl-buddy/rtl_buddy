"""Slurm dispatch backend unit tests (#351 P1): sbatch argv shape,
job-id capture, queue-drain polling, and cancellation — all against a
faked subprocess layer, no Slurm required."""

from __future__ import annotations

import math
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


@pytest.fixture(autouse=True)
def _no_inherited_cluster(monkeypatch):
    # $SBATCH_CLUSTERS selects a cluster exactly as `--clusters` does
    # (#509), so a developer or CI host that exports it would otherwise
    # change what every probe in this module asks for.
    monkeypatch.delenv("SBATCH_CLUSTERS", raising=False)


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


def _fake_run(calls, results, *, max_array_size=None, max_array_tasks=None):
    """subprocess.run stand-in: records argv, pops canned results.

    Any ``scontrol`` call — the MaxArraySize probe (#509), with or without
    the ``-M <cluster>`` a cross-cluster ``sbatch-args`` adds — is answered from
    ``max_array_size`` instead — neither recorded nor popped — so a test
    about sbatch argv keeps asserting on sbatch calls alone. ``None`` (the
    default) makes the probe fail, i.e. no chunking. The probe itself is
    covered by the tests in the chunking section, which record it.
    """

    def run(argv, capture_output=True, text=True, cwd=None, timeout=None):
        if list(argv[:1]) == ["scontrol"]:
            return _scontrol_result(max_array_size, max_array_tasks)
        calls.append(list(argv))
        result = (
            results.pop(0)
            if results
            else SimpleNamespace(returncode=0, stdout="", stderr="")
        )
        return result

    return run


def _scontrol_result(max_array_size, max_array_tasks=None):
    """`scontrol show config` output, or a failure when the limit is unknown.

    ``SchedulerParameters`` is always rendered — a real dump has the line
    whether or not it carries ``max_array_tasks`` — so the "not set" case
    is the realistic one rather than a line the parser never sees.
    """
    if max_array_size is None:
        return SimpleNamespace(
            returncode=1, stdout="", stderr="scontrol: error: Unable to contact slurm"
        )
    params = "bf_window=2880,default_queue_depth=100"
    if max_array_tasks is not None:
        params += f",max_array_tasks={max_array_tasks}"
    return SimpleNamespace(
        returncode=0,
        stdout=(
            "Configuration data as of 2026-08-31T12:00:00\n"
            "MaxArrayJobs            = 20\n"
            f"MaxArraySize            = {max_array_size}\n"
            "MaxDBDMsgs              = 20000\n"
            f"SchedulerParameters     = {params}\n"
        ),
        stderr="",
    )


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
        SimpleNamespace(
            returncode=0,
            stdout="1|Resources|PENDING|0:00|rb:basic\n2|None|RUNNING|0:12|rb:basic:1\n",
            stderr="",
        ),
        SimpleNamespace(
            returncode=0, stdout="2|None|RUNNING|0:22|rb:basic:1\n", stderr=""
        ),
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
                stdout=(
                    "7_[1-3]|DependencyNeverSatisfied|PENDING|0:00|rb:basic\n"
                    "9|Resources|PENDING|0:00|rb:basic:1\n"
                ),
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
                returncode=0,
                stdout="7|DependencyNeverSatisfied|PENDING|0:00|rb:basic\n",
                stderr="",
            ),
            SimpleNamespace(returncode=0, stdout="", stderr=""),  # scancel
        ],
    )
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    monkeypatch.setattr(slurm_module.time, "sleep", lambda s: None)
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.wait_all([JobHandle("7", _spec())])

    assert [argv[0] for argv in calls] == ["squeue", "scancel"]


def test_wait_all_asks_squeue_for_reason_state_time_and_name(monkeypatch):
    calls, results = ([], [SimpleNamespace(returncode=0, stdout="", stderr="")])
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    SlurmDispatchBackend(DispatchConfigFile().initialise()).wait_all(
        [JobHandle("1", _spec())]
    )
    # Reason drives dependency reaping; state/time/name drive the progress
    # line's running-vs-pending split and its longest-running job (#435).
    assert "--format=%i|%r|%T|%M|%j" in calls[0]


def test_wait_all_tolerates_a_short_squeue_line(monkeypatch):
    """A Slurm rendering fewer columns must not break the wait itself."""
    calls, results = (
        [],
        [
            SimpleNamespace(returncode=0, stdout="1|Resources\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ],
    )
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    monkeypatch.setattr(slurm_module.time, "sleep", lambda s: None)
    SlurmDispatchBackend(DispatchConfigFile().initialise()).wait_all(
        [JobHandle("1", _spec())]
    )
    assert len([argv for argv in calls if argv[0] == "squeue"]) == 2


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
            # JobID|State|ElapsedRaw|TimelimitRaw|AllocCPUS|ReqCPUS|ReqMem|TotalCPU|MaxRSS
            "500_1|COMPLETED|75|60|2|2|4G||",
            "500_1.batch|COMPLETED|75||2|2||01:02.500|2948K",
            "500_2|TIMEOUT|3600|60|2|2|4G||",
            "500_2.batch|CANCELLED|3600||2|2||59:00.000|1.5G",
            # A whole-core site: one cpu asked for, two handed out (#505).
            "42|COMPLETED|10|1|2|1|500M||",
            "42.batch|COMPLETED|10||2|1||00:03.250|10240K",
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
    assert t1["req_cpus"] == 2
    assert t1["req_mem_bytes"] == 4 * 2**30
    assert t1["max_rss_bytes"] == 2948 * 1024
    assert t1["total_cpu_s"] == 62.5

    t2 = telemetry["500_2"]
    assert t2["state"] == "TIMEOUT"
    assert t2["max_rss_bytes"] == int(1.5 * 2**30)

    assert telemetry["42"]["total_cpu_s"] == 3.25
    # The requested cpus are carried alongside the allocated ones: right-sizing
    # judges efficiency against what the reservation asked for, because that is
    # the number a tests.yaml edit can move (#505).
    assert telemetry["42"]["alloc_cpus"] == 2
    assert telemetry["42"]["req_cpus"] == 1


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
            "9|COMPLETED|100|60|4|4|4G||",
            "9.batch|COMPLETED|100||4|4||00:10.000|500M",
            "9.0|COMPLETED|100||4|4||01:30.000|900M",
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
    # Default concurrency leaves both the reservation and the argv as they
    # were before #495.
    assert "--parallel" not in wrapped


def test_submit_build_reserves_the_head_scaled_cpus(monkeypatch):
    """The backend submits what the head sized; it never re-scales (#495).

    The head folded `cfg-dispatch.compile.parallel` into `resources.cpus`
    (and capped it against the planned configs) before the spec got here,
    so the backend's only job is to emit both numbers: the reservation the
    job holds, and the concurrency it is allowed to spend it on.
    """
    from rtl_buddy.dispatch.base import BuildJobSpec

    calls, results = [], [SimpleNamespace(returncode=0, stdout="901\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    spec = BuildJobSpec(
        suite_dir="/proj/verif/blk",
        test_config_path="/proj/verif/blk/tests.yaml",
        resources=JobResources(cpus=16, mem="16G", time="02:00:00"),
        parallel=4,
    )
    backend.submit_build(spec)

    (argv,) = calls
    assert "--cpus-per-task=16" in argv
    # mem/time are NOT multiplied by the head, so they arrive as configured.
    assert "--mem=16G" in argv and "--time=02:00:00" in argv
    wrapped = shlex.split(argv[argv.index("--wrap") + 1])
    assert wrapped[wrapped.index("--parallel") + 1] == "4"


def test_build_submitted_event_records_the_concurrency(monkeypatch, caplog):
    """cpus alone cannot explain a 16-CPU build job; the pair can (#495)."""
    import logging

    from rtl_buddy.dispatch.base import BuildJobSpec
    from rtl_buddy.logging_utils import _human_message

    calls, results = [], [SimpleNamespace(returncode=0, stdout="902\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    with caplog.at_level(logging.INFO):
        backend.submit_build(
            BuildJobSpec(
                suite_dir="/proj/verif/blk",
                test_config_path="/proj/verif/blk/tests.yaml",
                resources=JobResources(cpus=16),
                parallel=4,
            )
        )
    (record,) = [
        r
        for r in caplog.records
        if r.__dict__.get("rtl_event") == "dispatch.build_submitted"
    ]
    fields = record.__dict__["rtl_fields"]
    assert (fields["cpus"], fields["parallel"]) == (16, 4)
    assert "4 builds at a time" in _human_message("dispatch.build_submitted", fields)
    # ...and the default reads exactly as it did before the knob existed.
    assert _human_message(
        "dispatch.build_submitted", {"job_id": "902", "suite_dir": "s", "parallel": 1}
    ) == ("Submitted shared-build job 902 for s")


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


def test_wait_all_matches_a_reason_rendered_with_surrounding_text(monkeypatch):
    """Substring, not equality: an exact match could regress into the poll."""
    calls, results = (
        [],
        [
            SimpleNamespace(
                returncode=0,
                stdout="7|(DependencyNeverSatisfied)|PENDING|0:00|rb:basic\n",
                stderr="",
            ),
            SimpleNamespace(returncode=0, stdout="", stderr=""),  # scancel
        ],
    )
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    monkeypatch.setattr(slurm_module.time, "sleep", lambda s: None)
    SlurmDispatchBackend(DispatchConfigFile().initialise()).wait_all(
        [JobHandle("7", _spec())]
    )
    assert [argv[0] for argv in calls] == ["squeue", "scancel"]


def test_wait_all_reports_a_failed_scancel(monkeypatch, caplog):
    """The jobs are already dropped from `remaining`, so a failed cancel leaves
    them queued after the run exits — that has to be recoverable by hand."""
    import logging

    calls, results = (
        [],
        [
            SimpleNamespace(
                returncode=0,
                stdout="7|DependencyNeverSatisfied|PENDING|0:00|rb:basic\n",
                stderr="",
            ),
            SimpleNamespace(
                returncode=1, stdout="", stderr="scancel: error: Invalid job id"
            ),
        ],
    )
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    monkeypatch.setattr(slurm_module.time, "sleep", lambda s: None)
    with caplog.at_level(logging.WARNING):
        SlurmDispatchBackend(DispatchConfigFile().initialise()).wait_all(
            [JobHandle("7", _spec())]
        )
    assert "still" in caplog.text and "queued" in caplog.text
    assert "Invalid job id" in caplog.text


# ------------- reaping afterok dependents when the build fails


def test_dependent_submit_asks_slurm_to_reap_on_invalid_dependency(monkeypatch):
    """cancel_all cannot run if the head is SIGKILLed, so Slurm must own it."""
    calls, results = ([], [SimpleNamespace(returncode=0, stdout="7", stderr="")])
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.submit(_spec(), dependency="42")

    (argv,) = calls
    assert "--dependency=afterok:42" in argv
    assert "--kill-on-invalid-dep=yes" in argv


def test_undependent_submit_does_not_pass_the_flag(monkeypatch):
    """It only means anything alongside a dependency."""
    calls, results = ([], [SimpleNamespace(returncode=0, stdout="7", stderr="")])
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.submit(_spec(), dependency=None)

    (argv,) = calls
    assert not [a for a in argv if a.startswith("--dependency")]
    assert "--kill-on-invalid-dep=yes" not in argv


def test_dependent_array_submit_asks_slurm_to_reap_too(tmp_path, monkeypatch):
    calls, results = ([], [SimpleNamespace(returncode=0, stdout="9", stderr="")])
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.submit_array(
        [_spec(run_id=1), _spec(run_id=2)],
        array_dir=tmp_path / "arr",
        dependency="42",
    )

    (argv,) = calls
    assert "--dependency=afterok:42" in argv
    assert "--kill-on-invalid-dep=yes" in argv


def test_undependent_array_submit_does_not_pass_the_flag(tmp_path, monkeypatch):
    calls, results = ([], [SimpleNamespace(returncode=0, stdout="9", stderr="")])
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.submit_array(
        [_spec(run_id=1), _spec(run_id=2)],
        array_dir=tmp_path / "arr",
        dependency=None,
    )

    (argv,) = calls
    # Both halves, so a bare `--dependency=afterok:None` cannot slip through.
    assert not [a for a in argv if a.startswith("--dependency")]
    assert "--kill-on-invalid-dep=yes" not in argv


def test_the_flag_precedes_user_sbatch_args(monkeypatch):
    """User sbatch-args come last so a site can still override the behaviour."""
    calls, results = ([], [SimpleNamespace(returncode=0, stdout="7", stderr="")])
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    cfg = DispatchConfigFile(sbatch_args=["--kill-on-invalid-dep=no"]).initialise()
    backend = SlurmDispatchBackend(cfg)

    backend.submit(_spec(), dependency="42")

    (argv,) = calls
    assert argv.index("--kill-on-invalid-dep=yes") < argv.index(
        "--kill-on-invalid-dep=no"
    )


# ---------------------------------- accounting sampling frequency (#365)


def test_per_second_task_accounting_is_requested_by_default(monkeypatch):
    """MaxRSS is a high-water mark over samples, and a sim job is usually
    shorter than the stock 30 s JobAcctGatherFrequency — so it is sampled
    once, near zero, and right-sizing advises from that (#365)."""
    calls, results = ([], [SimpleNamespace(returncode=0, stdout="7", stderr="")])
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    assert backend.accounting_interval_s() == 1.0
    backend.submit(_spec())
    (argv,) = calls
    assert "--acctg-freq=task=1" in argv


def test_a_configured_acctg_freq_is_left_alone(monkeypatch):
    """A site that must not raise the sampling rate says so in sbatch-args."""
    calls, results = ([], [SimpleNamespace(returncode=0, stdout="7", stderr="")])
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    cfg = DispatchConfigFile(sbatch_args=["--acctg-freq=task=15,energy=0"]).initialise()
    backend = SlurmDispatchBackend(cfg)

    # ...and right-sizing is told the rate that will really apply.
    assert backend.accounting_interval_s() == 15.0
    backend.submit(_spec())
    (argv,) = calls
    assert len([a for a in argv if a.startswith("--acctg-freq")]) == 1
    assert "--acctg-freq=task=15,energy=0" in argv


def test_a_separated_acctg_freq_value_is_recognised(monkeypatch):
    cfg = DispatchConfigFile(sbatch_args=["--acctg-freq", "30"]).initialise()
    backend = SlurmDispatchBackend(cfg)

    assert backend.accounting_interval_s() == 30.0
    assert "--acctg-freq=task=1" not in backend.sbatch_args


def test_arrays_carry_the_accounting_frequency_too(monkeypatch, tmp_path):
    calls, results = ([], [SimpleNamespace(returncode=0, stdout="55", stderr="")])
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.submit_array([_spec(run_id=1), _spec(run_id=2)], array_dir=tmp_path / "arr")

    (argv,) = calls
    assert "--acctg-freq=task=1" in argv


def test_task_sampling_interval_parsing():
    parse = slurm_module._task_sampling_interval
    assert parse("30") == 30.0
    assert parse("task=5") == 5.0
    assert parse("energy=30,task=2") == 2.0
    # An explicit disable is a KNOWN absence of sampling, not an unknown
    # interval: every peak must be distrusted, not trusted.
    assert parse("task=0") == math.inf
    # These say nothing about task sampling at all.
    assert parse("energy=30") is None
    assert parse("task=nonsense") is None


def test_a_flag_that_says_nothing_about_task_sampling_does_not_disarm_the_guard(
    monkeypatch, caplog
):
    """`--acctg-freq=energy=30` is about a different datatype. Deferring to it
    would leave tasks on the site default AND report the interval as unknown,
    which right-sizing reads as "trust the peak" — #365 back, both guards
    disarmed by one flag neither guard was about."""
    import logging

    cfg = DispatchConfigFile(sbatch_args=["--acctg-freq=energy=30"]).initialise()
    with caplog.at_level(logging.WARNING):
        backend = SlurmDispatchBackend(cfg)

    assert backend.accounting_interval_s() == 1.0
    assert "--acctg-freq=task=1" in backend.sbatch_args
    assert "says nothing about task sampling" in caplog.text
    # The user's own flag is still passed through, and still wins.
    assert backend.sbatch_args.index("--acctg-freq=task=1") < backend.sbatch_args.index(
        "--acctg-freq=energy=30"
    )


def test_disabled_task_accounting_is_reported_as_never_sampled(monkeypatch):
    cfg = DispatchConfigFile(sbatch_args=["--acctg-freq=task=0"]).initialise()
    backend = SlurmDispatchBackend(cfg)

    # inf, not None: no elapsed time can exceed it, so no peak is trusted.
    assert backend.accounting_interval_s() == math.inf
    assert "--acctg-freq=task=1" not in backend.sbatch_args


# ------------------------------- #435: progress, job counting, max-wait


def test_expand_squeue_id_covers_every_shape_squeue_speaks():
    expand = slurm_module._expand_squeue_id
    # A non-array id is itself.
    assert expand("1235") == ["1235"]
    # One element of an array is itself.
    assert expand("1235_3") == ["1235_3"]
    # A pending range is every element it holds...
    assert expand("1235_[1-3]") == ["1235_1", "1235_2", "1235_3"]
    # ...including a mixed list...
    assert expand("1235_[1,3-5]") == ["1235_1", "1235_3", "1235_4", "1235_5"]
    # ...and the throttle suffix is not an element.
    assert expand("1235_[1-2%4]") == ["1235_1", "1235_2"]
    # A bare base id with array handles is conservatively all of them: the
    # alternative reports a 40-element array as one outstanding job.
    assert expand("1235", ["1235_1", "1235_2", "9"]) == ["1235_1", "1235_2"]


def test_wait_all_counts_handles_not_queue_lines(monkeypatch, caplog):
    """One pending array line stands for as many jobs as it has elements."""
    import logging

    calls, results = (
        [],
        [
            SimpleNamespace(
                returncode=0,
                stdout="9_[1-3]|Priority|PENDING|0:00|rb:basic\n",
                stderr="",
            ),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ],
    )
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    monkeypatch.setattr(slurm_module.time, "sleep", lambda s: None)
    handles = [JobHandle(f"9_{i}", _spec(run_id=i)) for i in (1, 2, 3)]

    with caplog.at_level(logging.INFO):
        SlurmDispatchBackend(DispatchConfigFile().initialise()).wait_all(handles)

    progress = [
        r for r in caplog.records if r.__dict__.get("rtl_event") == "dispatch.progress"
    ]
    assert progress, "expected a progress record"
    assert progress[0].__dict__["rtl_fields"]["remaining"] == 3
    assert progress[0].__dict__["rtl_fields"]["total"] == 3
    assert progress[0].__dict__["rtl_fields"]["pending"] == 3


def test_progress_names_the_longest_running_job(monkeypatch, caplog):
    import logging

    calls, results = (
        [],
        [
            SimpleNamespace(
                returncode=0,
                stdout=(
                    "9_1|None|RUNNING|8:02|rb:demo_alu\n"
                    "9_2|None|RUNNING|0:11|rb:demo_fifo\n"
                    "9_3|Priority|PENDING|0:00|rb:demo_mem\n"
                ),
                stderr="",
            ),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ],
    )
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    monkeypatch.setattr(slurm_module.time, "sleep", lambda s: None)
    handles = [JobHandle(f"9_{i}", _spec(run_id=i)) for i in (1, 2, 3)]

    with caplog.at_level(logging.INFO):
        SlurmDispatchBackend(DispatchConfigFile().initialise()).wait_all(handles)

    fields = [
        r.__dict__["rtl_fields"]
        for r in caplog.records
        if r.__dict__.get("rtl_event") == "dispatch.progress"
    ][0]
    assert (fields["running"], fields["pending"]) == (2, 1)
    assert fields["longest_job"] == "rb:demo_alu"
    assert fields["longest_s"] == 482.0


def test_max_wait_fails_loud_with_the_outstanding_ids(monkeypatch, caplog):
    """An unbounded wait turns a stuck queue into a silent hang (#435)."""
    import logging

    never_drains = SimpleNamespace(
        returncode=0, stdout="9_[1-3]|Priority|PENDING|0:00|rb:basic\n", stderr=""
    )
    calls = []

    def run(argv, capture_output=True, text=True, cwd=None, timeout=None):
        calls.append(list(argv))
        return never_drains

    monkeypatch.setattr(slurm_module.subprocess, "run", run)
    # Every sleep advances a fake clock past the deadline.
    clock = {"now": 0.0}
    monkeypatch.setattr(
        slurm_module.time,
        "sleep",
        lambda s: clock.__setitem__("now", clock["now"] + 100),
    )
    monkeypatch.setattr(slurm_module.time, "monotonic", lambda: clock["now"])

    cfg = DispatchConfigFile(max_wait=60.0).initialise()
    handles = [JobHandle(f"9_{i}", _spec(run_id=i)) for i in (1, 2, 3)]
    with caplog.at_level(logging.WARNING):
        with pytest.raises(FatalRtlBuddyError) as excinfo:
            SlurmDispatchBackend(cfg).wait_all(handles)

    assert "9_[1-3]" in str(excinfo.value)
    assert "max-wait" in str(excinfo.value)
    warnings = [
        r
        for r in caplog.records
        if r.__dict__.get("rtl_event") == "dispatch.max_wait_exceeded"
    ]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].__dict__["rtl_fields"]["jobs"] == ["9_[1-3]"]


def test_cancelled_warning_carries_the_grouped_ids(monkeypatch, caplog):
    import logging

    calls, results = [], []
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    with caplog.at_level(logging.WARNING):
        backend.cancel_all(
            [
                JobHandle("500_1", _spec()),
                JobHandle("500_2", _spec()),
                JobHandle("42", _spec()),
            ]
        )

    (record,) = [
        r for r in caplog.records if r.__dict__.get("rtl_event") == "dispatch.cancelled"
    ]
    assert record.__dict__["rtl_fields"]["job_ids"] == ["500_[1-2]", "42"]
    assert "500_[1-2]" in record.message


# ------------------------------------------- #405: retry backoff via --begin


def test_retry_delay_becomes_a_begin_flag(monkeypatch):
    """The scheduler serves the backoff, so the delayed job holds nothing.

    Sleeping in the head would keep the reservation the license pool needs
    to drain; ``--begin`` leaves the job PENDING instead.
    """
    calls, results = [], [SimpleNamespace(returncode=0, stdout="9\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.submit(_spec(), delay_sec=90.0)

    (argv,) = calls
    assert "--begin=now+90" in argv


def test_no_begin_flag_without_a_delay(monkeypatch):
    calls, results = [], [SimpleNamespace(returncode=0, stdout="9\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.submit(_spec())

    (argv,) = calls
    assert not any(a.startswith("--begin") for a in argv)


@pytest.mark.parametrize(
    "delay, expected",
    [
        (0.0, None),
        (0.4, None),  # rounds to 0 s: an inert `now+0` is not worth emitting
        (1.6, "--begin=now+2"),
        (63.2, "--begin=now+63"),
        (600.0, "--begin=now+600"),
    ],
)
def test_begin_flag_rounds_to_whole_seconds(monkeypatch, delay, expected):
    calls, results = [], [SimpleNamespace(returncode=0, stdout="9\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.submit(_spec(), delay_sec=delay)

    (argv,) = calls
    begin = [a for a in argv if a.startswith("--begin")]
    assert begin == ([expected] if expected else [])


def test_delayed_submit_still_carries_reservation_and_dependency(monkeypatch):
    """A retry is a normal submit plus a hold — nothing else changes."""
    calls, results = [], [SimpleNamespace(returncode=0, stdout="9\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(
        DispatchConfigFile(sbatch_args=["--partition=verif"]).initialise()
    )

    backend.submit(_spec(), dependency="88", delay_sec=30.0)

    (argv,) = calls
    assert "--begin=now+30" in argv
    assert "--dependency=afterok:88" in argv
    assert "--time=01:00:00" in argv
    assert "--partition=verif" in argv
    # sbatch-args stay last, so a site value still wins any duplicate.
    assert argv.index("--begin=now+30") < argv.index("--partition=verif")


def test_submitted_event_records_the_backoff(monkeypatch, caplog):
    import logging

    calls, results = [], [SimpleNamespace(returncode=0, stdout="9\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    with caplog.at_level(logging.INFO):
        backend.submit(_spec(), delay_sec=45.0)

    (record,) = [
        r for r in caplog.records if r.__dict__.get("rtl_event") == "dispatch.submitted"
    ]
    assert record.__dict__["rtl_fields"]["begin_delay_sec"] == 45.0


def test_max_wait_is_widened_by_a_backoff_the_head_asked_for(monkeypatch):
    """A job held on ``--begin`` is PENDING for the whole backoff.

    squeue reports it outstanding all that time, so charging the hold
    against max-wait would fail every retry round whose backoff is longer
    than max-wait, before the job had been allowed to start (#405 review).
    """
    pending = SimpleNamespace(
        returncode=0, stdout="9|BeginTime|PENDING|0:00|rb:basic\n", stderr=""
    )
    drained = SimpleNamespace(returncode=0, stdout="", stderr="")
    clock = {"now": 0.0}

    def _install(monkeypatch):
        """Fresh squeue transcript + fake clock: pending twice, then gone."""
        clock["now"] = 0.0
        polls = iter([pending, pending, drained])
        monkeypatch.setattr(
            slurm_module.subprocess,
            "run",
            lambda argv, capture_output=True, text=True, cwd=None, timeout=None: next(
                polls
            ),
        )
        monkeypatch.setattr(
            slurm_module.time,
            "sleep",
            lambda s: clock.__setitem__("now", clock["now"] + 100),
        )
        monkeypatch.setattr(slurm_module.time, "monotonic", lambda: clock["now"])

    cfg = DispatchConfigFile(max_wait=60.0).initialise()
    handles = [JobHandle("9", _spec())]

    # 100 s of held-and-pending is past a bare 60 s budget...
    _install(monkeypatch)
    with pytest.raises(FatalRtlBuddyError, match="max-wait"):
        SlurmDispatchBackend(cfg).wait_all(handles)

    # ...but not past 60 s plus the 600 s hold the head itself imposed.
    _install(monkeypatch)
    SlurmDispatchBackend(cfg).wait_all(handles, extra_wait=600.0)
    assert clock["now"] == 200.0


# ------- #505 review: why SBATCH_CPUS_PER_TASK is not treated as an override


def test_every_submit_path_states_cpus_per_task_on_the_command_line(
    monkeypatch, tmp_path
):
    """Load-bearing for right-sizing, not just cosmetic (#505 review).

    sbatch's documented precedence is command line > environment > script,
    so `SBATCH_CPUS_PER_TASK` in a site's environment is always beaten by
    the flag rtl-buddy itself passes — which is why
    `cpu_request_overrides()` deliberately does NOT treat that variable as
    an override. Make any of these three paths emit the flag conditionally
    and that reasoning stops holding, so pin all three here rather than
    discover it through wrong advice.
    """
    from rtl_buddy.dispatch.base import BuildJobSpec

    def _argv_of(submit):
        calls, results = [], [SimpleNamespace(returncode=0, stdout="7\n", stderr="")]
        monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
        backend = SlurmDispatchBackend(DispatchConfigFile().initialise())
        submit(backend)
        (argv,) = calls
        return argv

    sim = _argv_of(lambda b: b.submit(_spec()))
    array = _argv_of(
        lambda b: b.submit_array(
            [_spec(run_id=i) for i in (1, 2)],
            array_dir=tmp_path / "array",
            max_parallel=2,
        )
    )
    build = _argv_of(
        lambda b: b.submit_build(
            BuildJobSpec(
                suite_dir="/proj/verif/blk",
                test_config_path="/proj/verif/blk/tests.yaml",
                resources=JobResources(cpus=8, mem="16G", time="02:00:00"),
                reg_level=0,
                log_path=None,
            )
        )
    )

    for argv in (sim, array, build):
        assert any(a.startswith("--cpus-per-task=") for a in argv), argv
        # ...and none of them states the task or node counts, which is why
        # the SBATCH_* variables for THOSE do reach sbatch and are treated
        # as overrides.
        assert not any(
            a.startswith(("--ntasks", "-n", "--nodes", "-N")) for a in argv
        ), argv


def test_effective_sbatch_args_is_what_the_backend_will_append():
    """Right-sizing reads its cpu overrides from here, so it must be real.

    The backend is built once from the orchestration config, before the
    suite loop, and keeps that list however `root_cfg` is later rebuilt —
    which is exactly why the snapshot is taken from the backend and not
    from whichever `cfg-dispatch` is current (#505 review).
    """
    backend = SlurmDispatchBackend(
        DispatchConfigFile(sbatch_args=["--partition=verif", "--ntasks=4"]).initialise()
    )
    args = backend.effective_sbatch_args
    assert "--partition=verif" in args and "--ntasks=4" in args
    # ...including the accounting rate it prepends, since that is submitted too.
    assert any(a.startswith("--acctg-freq") for a in args)
    # It is the same list every submission appends, not a copy taken early.
    assert args is backend.sbatch_args

    bare = SlurmDispatchBackend(DispatchConfigFile().initialise())
    assert not [a for a in bare.effective_sbatch_args if not a.startswith("--acctg")]


# ------------------------------------- MaxArraySize chunking (#509)


def _events(caplog, event):
    """The `rtl_fields` of every record logged for one rtl_event."""
    return [
        r.__dict__["rtl_fields"]
        for r in caplog.records
        if r.__dict__.get("rtl_event") == event
    ]


def _array_ranges(calls):
    """The `--array=` value of every sbatch call, in submission order."""
    return [
        arg.split("=", 1)[1]
        for argv in calls
        for arg in argv
        if arg.startswith("--array=")
    ]


def test_a_group_larger_than_max_array_size_is_split_across_arrays(
    monkeypatch, tmp_path
):
    """`sbatch --array=1-1128` on a MaxArraySize=1001 cluster is refused and
    the whole run dies; the group must be chunked instead (#509)."""
    calls = []
    results = [
        SimpleNamespace(returncode=0, stdout=f"{base}\n", stderr="")
        for base in (100, 101, 102)
    ]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    # max-array-size is Slurm's MaxArraySize: the largest task index is one
    # below it, so 5 means at most four elements per array.
    backend = SlurmDispatchBackend(DispatchConfigFile(max_array_size=5).initialise())

    specs = [_spec(run_id=i) for i in range(1, 11)]
    array_dir = tmp_path / "array-001"
    handles = backend.submit_array(specs, array_dir=array_dir)

    assert _array_ranges(calls) == ["1-4", "1-4", "1-2"]
    # Concatenated in spec order, so collection sees one logical group.
    assert [h.job_id for h in handles] == [
        "100_1",
        "100_2",
        "100_3",
        "100_4",
        "101_1",
        "101_2",
        "101_3",
        "101_4",
        "102_1",
        "102_2",
    ]
    assert [h.spec for h in handles] == specs

    # One manifest per slice, each covering exactly its own elements, so %a
    # still maps 1:1 onto a manifest line.
    for index, expected in ((1, 4), (2, 4), (3, 2)):
        slice_dir = array_dir / f"slice-{index}"
        assert len((slice_dir / "manifest.txt").read_text().splitlines()) == expected
        assert (slice_dir / "array.sh").read_text().startswith("#!/bin/bash")
        assert f"--output={slice_dir}/slurm-%a.log" in calls[index - 1]
        assert calls[index - 1][-2:] == [
            str(slice_dir / "array.sh"),
            str(slice_dir / "manifest.txt"),
        ]
    assert "--run-id 5" in (array_dir / "slice-2" / "manifest.txt").read_text()

    # Element logs never collide across slices: each is under its own slice.
    assert specs[0].log_path == array_dir / "slice-1" / "slurm-1.log"
    assert specs[4].log_path == array_dir / "slice-2" / "slurm-1.log"
    assert specs[9].log_path == array_dir / "slice-3" / "slurm-2.log"

    # The job name says which slice of the group this array is.
    names = [a for argv in calls for a in argv if a.startswith("--job-name=")]
    assert names == [
        "--job-name=rb:basic+3/1",
        "--job-name=rb:basic+3/2",
        "--job-name=rb:basic+1/3",
    ]


def test_a_group_within_the_limit_keeps_todays_single_array_layout(
    monkeypatch, tmp_path
):
    calls, results = [], [SimpleNamespace(returncode=0, stdout="500\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile(max_array_size=11).initialise())

    specs = [_spec(run_id=i) for i in range(1, 11)]
    array_dir = tmp_path / "array-001"
    backend.submit_array(specs, array_dir=array_dir)

    assert _array_ranges(calls) == ["1-10"]
    # No slice-N/ subdirectory: unchunked artefact paths do not move.
    assert (array_dir / "manifest.txt").exists()
    assert not (array_dir / "slice-1").exists()
    assert specs[0].log_path == array_dir / "slurm-1.log"


def test_an_unknown_limit_submits_one_array_as_before(monkeypatch, tmp_path, caplog):
    """No `scontrol` and no config override: chunking is off, not guessed."""
    calls, results = [], [SimpleNamespace(returncode=0, stdout="500\n", stderr="")]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    with caplog.at_level("INFO"):
        backend.submit_array(
            [_spec(run_id=i) for i in range(1, 11)], array_dir=tmp_path / "arr"
        )

    assert _array_ranges(calls) == ["1-10"]
    assert len(_events(caplog, "dispatch.max_array_size_unknown")) == 1
    # ...and the single-array event still reports itself as one slice of one.
    (fields,) = _events(caplog, "dispatch.array_submitted")
    assert (fields["slice"], fields["slices"]) == (1, 1)


def test_the_throttle_applies_per_slice(monkeypatch, tmp_path):
    calls = []
    results = [
        SimpleNamespace(returncode=0, stdout=f"{base}\n", stderr="")
        for base in (100, 101, 102)
    ]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile(max_array_size=5).initialise())

    backend.submit_array(
        [_spec(run_id=i) for i in range(1, 11)],
        array_dir=tmp_path / "arr",
        max_parallel=3,
    )
    # %N caps each array, so the group's peak concurrency is slices x N; the
    # last slice is smaller than the cap and needs no throttle at all.
    assert _array_ranges(calls) == ["1-4%3", "1-4%3", "1-2"]


def test_max_array_size_is_read_from_scontrol_once_per_backend(monkeypatch, tmp_path):
    calls = []
    results = [
        SimpleNamespace(returncode=0, stdout=f"{base}\n", stderr="")
        for base in (100, 101, 102, 103)
    ]
    probes = []

    def run(argv, capture_output=True, text=True, cwd=None, timeout=None):
        if list(argv[:2]) == ["scontrol", "show"]:
            probes.append((list(argv), cwd, timeout))
            return _scontrol_result(4)
        return _fake_run(calls, results)(
            argv, capture_output=capture_output, text=text, cwd=cwd, timeout=timeout
        )

    monkeypatch.setattr(slurm_module.subprocess, "run", run)
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.submit_array(
        [_spec(run_id=i) for i in (1, 2, 3, 4)], array_dir=tmp_path / "a"
    )
    backend.submit_array(
        [_spec(run_id=i) for i in (5, 6, 7, 8)], array_dir=tmp_path / "b"
    )

    # MaxArraySize 4 => task indices 0..3 => at most three 1-based elements.
    assert _array_ranges(calls) == ["1-3", "1-1", "1-3", "1-1"]
    # Resolved once and cached: the second group re-uses the first probe.
    assert len(probes) == 1
    argv, cwd, timeout = probes[0]
    assert argv == ["scontrol", "show", "config"]
    assert cwd == "/proj/verif/blk"  # explicit, per the engineering guidelines
    assert timeout is not None  # time-boxed: a wedged scontrol must not hang


def test_the_configured_limit_wins_over_scontrol(monkeypatch, tmp_path):
    """A site whose submit host has no usable `scontrol` pins the value."""
    calls = []
    results = [
        SimpleNamespace(returncode=0, stdout=f"{base}\n", stderr="")
        for base in (100, 101)
    ]
    probed = []

    def run(argv, capture_output=True, text=True, cwd=None, timeout=None):
        if list(argv[:2]) == ["scontrol", "show"]:
            probed.append(list(argv))
            return _scontrol_result(1001)
        return _fake_run(calls, results)(argv, cwd=cwd)

    monkeypatch.setattr(slurm_module.subprocess, "run", run)
    backend = SlurmDispatchBackend(DispatchConfigFile(max_array_size=3).initialise())

    backend.submit_array(
        [_spec(run_id=i) for i in (1, 2, 3, 4)], array_dir=tmp_path / "a"
    )

    assert _array_ranges(calls) == ["1-2", "1-2"]
    assert probed == []


def test_an_unusable_scontrol_answer_disables_chunking_loudly(
    monkeypatch, tmp_path, caplog
):
    """rc=0 but no MaxArraySize line: unknown, and said so once."""
    calls, results = [], [SimpleNamespace(returncode=0, stdout="7\n", stderr="")]

    def run(argv, capture_output=True, text=True, cwd=None, timeout=None):
        if list(argv[:2]) == ["scontrol", "show"]:
            return SimpleNamespace(
                returncode=0, stdout="SlurmVersion = 24.05\n", stderr=""
            )
        return _fake_run(calls, results)(argv, cwd=cwd)

    monkeypatch.setattr(slurm_module.subprocess, "run", run)
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    with caplog.at_level("INFO"):
        backend.submit_array(
            [_spec(run_id=i) for i in (1, 2, 3)], array_dir=tmp_path / "a"
        )

    assert _array_ranges(calls) == ["1-3"]
    # "Loudly" is the whole point: silence here is a cluster whose groups
    # will be refused by sbatch with nothing in the log to say why.
    (fields,) = _events(caplog, "dispatch.max_array_size_unknown")
    assert "no usable MaxArraySize" in fields["error"]


def test_a_wedged_scontrol_does_not_fail_the_submit(monkeypatch, tmp_path, caplog):
    calls, results = [], [SimpleNamespace(returncode=0, stdout="7\n", stderr="")]

    def run(argv, capture_output=True, text=True, cwd=None, timeout=None):
        if list(argv[:2]) == ["scontrol", "show"]:
            raise slurm_module.subprocess.TimeoutExpired(cmd=argv, timeout=timeout or 0)
        return _fake_run(calls, results)(argv, cwd=cwd)

    monkeypatch.setattr(slurm_module.subprocess, "run", run)
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    with caplog.at_level("INFO"):
        backend.submit_array(
            [_spec(run_id=i) for i in (1, 2, 3)], array_dir=tmp_path / "a"
        )

    assert _array_ranges(calls) == ["1-3"]
    assert len(_events(caplog, "dispatch.max_array_size_unknown")) == 1


def test_a_refused_array_names_the_unread_limit(monkeypatch, tmp_path):
    """The reporter's exact failure, made actionable on the console.

    `Invalid job array specification` IS an oversized group, and the probe
    that would have split it only says so at INFO — which a default console
    never shows. The error that fails the run carries the fix instead.
    """
    calls = []
    results = [
        SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="sbatch: error: Batch job submission failed: "
            "Invalid job array specification",
        )
    ]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    with pytest.raises(FatalRtlBuddyError) as excinfo:
        backend.submit_array(
            [_spec(run_id=i) for i in range(1, 4)], array_dir=tmp_path / "arr"
        )
    assert "Invalid job array specification" in str(excinfo.value)
    assert "cfg-dispatch.max-array-size" in str(excinfo.value)


def test_an_unrelated_submit_failure_offers_no_red_herring(monkeypatch, tmp_path):
    """An unknown limit is not a reason to blame every rejected submit.

    An invalid account/partition/QoS has its own recovery action, and a
    MaxArraySize hint stapled to it buries the sentence that matters.
    """
    calls = []
    results = [
        SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="sbatch: error: Batch job submission failed: "
            "Invalid partition name specified",
        )
    ]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    # The limit is unknown here — the other guard alone must not be what
    # keeps the hint away.
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    with pytest.raises(FatalRtlBuddyError) as excinfo:
        backend.submit_array(
            [_spec(run_id=i) for i in range(1, 4)], array_dir=tmp_path / "arr"
        )
    assert "Invalid partition name" in str(excinfo.value)
    assert "max-array-size" not in str(excinfo.value)


def test_a_refused_array_within_a_known_limit_points_at_the_override(
    monkeypatch, tmp_path
):
    """The array was inside everything the probe could see, and still refused.

    So the cluster enforces something it did not report, and the sentence
    must say that rather than claim the limit could not be read — while
    still naming the one knob that fixes it (#509 review).
    """
    calls = []
    results = [
        SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="sbatch: error: Batch job submission failed: "
            "Invalid job array specification",
        )
    ]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile(max_array_size=1001).initialise())

    with pytest.raises(FatalRtlBuddyError) as excinfo:
        backend.submit_array(
            [_spec(run_id=i) for i in range(1, 4)], array_dir=tmp_path / "arr"
        )
    message = str(excinfo.value)
    assert "Invalid job array specification" in message
    # 1000 = the pinned MaxArraySize of 1001 minus its exclusive index bound.
    assert "1000 element(s) per array" in message
    assert "read from config" in message
    assert "lower cfg-dispatch.max-array-size" in message
    # ...and NOT the unknown-limit sentence, which would be false here.
    assert "could not be read" not in message


def test_a_failed_slice_cancels_the_slices_already_submitted(monkeypatch, tmp_path):
    """The caller only learns of handles this call RETURNS, so a mid-group
    failure has to clean up its own earlier slices or orphan them."""
    calls = []
    results = [
        SimpleNamespace(returncode=0, stdout="100\n", stderr=""),
        SimpleNamespace(returncode=1, stdout="", stderr="Invalid job array"),
    ]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    backend = SlurmDispatchBackend(DispatchConfigFile(max_array_size=5).initialise())

    with pytest.raises(FatalRtlBuddyError, match="sbatch array submit failed"):
        backend.submit_array(
            [_spec(run_id=i) for i in range(1, 11)], array_dir=tmp_path / "arr"
        )

    assert calls[-1] == ["scancel", "100"]


def test_selected_cluster_reads_every_spelling_and_takes_the_last():
    """sbatch takes four forms and lets a later one override an earlier."""
    parse = slurm_module._selected_cluster
    assert parse([]) is None
    assert parse(["--partition=verif"]) is None
    assert parse(["-M", "remote"]) == "remote"
    assert parse(["-Mremote"]) == "remote"
    assert parse(["--clusters=remote"]) == "remote"
    assert parse(["--clusters", "remote"]) == "remote"
    assert parse(["--cluster=remote"]) == "remote"
    # A project appending an override to a shared list must win here for
    # the same reason it wins at submit.
    assert parse(["-M", "first", "--clusters=second"]) == "second"
    # A dangling option selects nothing rather than eating the next flag.
    assert parse(["--partition=verif", "-M"]) is None


def test_the_probe_asks_the_cluster_the_jobs_are_submitted_to(monkeypatch, tmp_path):
    """`scontrol show config` unqualified reads the LOCAL cluster, whose
    MaxArraySize is not the one the arrays are submitted against (#509)."""
    calls = []
    results = [
        SimpleNamespace(returncode=0, stdout=f"{base}\n", stderr="")
        for base in (100, 101)
    ]
    probes = []

    def run(argv, capture_output=True, text=True, cwd=None, timeout=None):
        if list(argv[:1]) == ["scontrol"]:
            probes.append(list(argv))
            return _scontrol_result(3)
        return _fake_run(calls, results)(argv, cwd=cwd)

    monkeypatch.setattr(slurm_module.subprocess, "run", run)
    cfg = DispatchConfigFile(sbatch_args=["--clusters=remote"]).initialise()
    backend = SlurmDispatchBackend(cfg)

    assert backend.cluster == "remote"
    backend.submit_array(
        [_spec(run_id=i) for i in (1, 2, 3, 4)], array_dir=tmp_path / "a"
    )

    assert probes == [["scontrol", "-M", "remote", "show", "config"]]
    # ...and the remote cluster's answer is what chunks the group.
    assert _array_ranges(calls) == ["1-2", "1-2"]


def test_several_clusters_leave_the_limit_unknown(monkeypatch, tmp_path, caplog):
    """`--clusters=a,b` is resolved at submit, so no single limit applies."""
    calls, results = [], [SimpleNamespace(returncode=0, stdout="7\n", stderr="")]
    probed = []

    def run(argv, capture_output=True, text=True, cwd=None, timeout=None):
        if list(argv[:1]) == ["scontrol"]:
            probed.append(list(argv))
            return _scontrol_result(1001)
        return _fake_run(calls, results)(argv, cwd=cwd)

    monkeypatch.setattr(slurm_module.subprocess, "run", run)
    cfg = DispatchConfigFile(sbatch_args=["-M", "alpha,beta"]).initialise()
    backend = SlurmDispatchBackend(cfg)

    with caplog.at_level("INFO"):
        backend.submit_array(
            [_spec(run_id=i) for i in (1, 2, 3)], array_dir=tmp_path / "a"
        )

    assert _array_ranges(calls) == ["1-3"]
    # Probing either one would pin a limit the other may not have.
    assert probed == []
    (fields,) = _events(caplog, "dispatch.max_array_size_unknown")
    assert "alpha,beta" in fields["error"]
    assert fields["cluster"] == "alpha,beta"
    assert "cfg-dispatch.max-array-size" in fields["hint"]


def test_a_pinned_limit_needs_no_cluster_probe(monkeypatch, tmp_path):
    """The config value is the answer for whichever cluster is selected."""
    calls, results = [], [SimpleNamespace(returncode=0, stdout="7\n", stderr="")]
    probed = []

    def run(argv, capture_output=True, text=True, cwd=None, timeout=None):
        if list(argv[:1]) == ["scontrol"]:
            probed.append(list(argv))
            return _scontrol_result(1001)
        return _fake_run(calls, results)(argv, cwd=cwd)

    monkeypatch.setattr(slurm_module.subprocess, "run", run)
    cfg = DispatchConfigFile(
        sbatch_args=["-M", "alpha,beta"], max_array_size=4
    ).initialise()
    backend = SlurmDispatchBackend(cfg)

    backend.submit_array([_spec(run_id=i) for i in (1, 2, 3)], array_dir=tmp_path / "a")
    assert probed == []
    assert _array_ranges(calls) == ["1-3"]


def test_an_ambiguous_cluster_still_earns_the_submit_failure_hint(
    monkeypatch, tmp_path
):
    """Unknown is unknown, however it became unknown."""
    calls = []
    results = [
        SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="sbatch: error: Batch job submission failed: "
            "Invalid job array specification",
        )
    ]
    monkeypatch.setattr(slurm_module.subprocess, "run", _fake_run(calls, results))
    cfg = DispatchConfigFile(sbatch_args=["--clusters=alpha,beta"]).initialise()
    backend = SlurmDispatchBackend(cfg)

    with pytest.raises(FatalRtlBuddyError) as excinfo:
        backend.submit_array(
            [_spec(run_id=i) for i in range(1, 4)], array_dir=tmp_path / "arr"
        )
    assert "cfg-dispatch.max-array-size" in str(excinfo.value)


def _probe_recording_run(calls, results, *, max_array_size, probes):
    """A fake subprocess.run that RECORDS the scontrol probe argv."""

    def run(argv, capture_output=True, text=True, cwd=None, timeout=None):
        if list(argv[:1]) == ["scontrol"]:
            probes.append(list(argv))
            return _scontrol_result(max_array_size)
        return _fake_run(calls, results)(argv, cwd=cwd)

    return run


def test_the_cluster_can_come_from_the_environment(monkeypatch, tmp_path):
    """Slurm reads $SBATCH_CLUSTERS as `--clusters`, so the probe must too."""
    calls, results, probes = [], [], []
    monkeypatch.setattr(
        slurm_module.subprocess,
        "run",
        _probe_recording_run(calls, results, max_array_size=3, probes=probes),
    )
    monkeypatch.setenv("SBATCH_CLUSTERS", "from-env")
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    assert backend.cluster == "from-env"
    backend._max_elements_per_array(cwd="/proj/verif/blk")
    assert probes == [["scontrol", "-M", "from-env", "show", "config"]]


def test_sbatch_args_beat_the_environment(monkeypatch, tmp_path):
    """Slurm gives the command line precedence; so does the probe."""
    calls, results, probes = [], [], []
    monkeypatch.setattr(
        slurm_module.subprocess,
        "run",
        _probe_recording_run(calls, results, max_array_size=3, probes=probes),
    )
    monkeypatch.setenv("SBATCH_CLUSTERS", "from-env")
    cfg = DispatchConfigFile(sbatch_args=["--clusters=from-args"]).initialise()
    backend = SlurmDispatchBackend(cfg)

    assert backend.cluster == "from-args"
    backend._max_elements_per_array(cwd="/proj/verif/blk")
    assert probes == [["scontrol", "-M", "from-args", "show", "config"]]


def test_a_blank_environment_selection_means_the_local_cluster(monkeypatch):
    """An exported-but-empty variable selects nothing, as it does for sbatch."""
    calls, results, probes = [], [], []
    monkeypatch.setattr(
        slurm_module.subprocess,
        "run",
        _probe_recording_run(calls, results, max_array_size=3, probes=probes),
    )
    monkeypatch.setenv("SBATCH_CLUSTERS", "   ")
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    assert backend.cluster is None
    backend._max_elements_per_array(cwd="/proj/verif/blk")
    assert probes == [["scontrol", "show", "config"]]


def test_several_clusters_in_the_environment_leave_the_limit_unknown(
    monkeypatch, caplog
):
    calls, results, probes = [], [], []
    monkeypatch.setattr(
        slurm_module.subprocess,
        "run",
        _probe_recording_run(calls, results, max_array_size=1001, probes=probes),
    )
    monkeypatch.setenv("SBATCH_CLUSTERS", "alpha,beta")
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    with caplog.at_level("INFO"):
        assert backend._max_elements_per_array(cwd="/proj/verif/blk") is None
    assert probes == []
    (fields,) = _events(caplog, "dispatch.max_array_size_unknown")
    assert fields["cluster"] == "alpha,beta"


def test_the_reserved_all_selection_leaves_the_limit_unknown(monkeypatch, caplog):
    """`--clusters=all` names no single cluster (#509 review).

    `scontrol -M all show config` answers with one config block per
    cluster, so a first-match regex would pin whichever sorted first — a
    limit belonging to a cluster the array may never be submitted to.
    """
    calls, results, probes = [], [], []
    monkeypatch.setattr(
        slurm_module.subprocess,
        "run",
        _probe_recording_run(calls, results, max_array_size=1001, probes=probes),
    )
    cfg = DispatchConfigFile(sbatch_args=["--clusters=all"]).initialise()
    backend = SlurmDispatchBackend(cfg)

    with caplog.at_level("INFO"):
        assert backend._max_elements_per_array(cwd="/proj/verif/blk") is None
    assert probes == []
    (fields,) = _events(caplog, "dispatch.max_array_size_unknown")
    assert fields["cluster"] == "all"
    assert "cfg-dispatch.max-array-size" in fields["hint"]


@pytest.mark.parametrize(
    "sbatch_args,env,expected",
    [
        ([], None, None),
        (["-M", "remote"], None, "remote"),
        ([], "from-env", "from-env"),
        (["--clusters=alpha,beta"], None, None),
        (["--clusters=all"], None, None),
        (["--clusters=ALL"], None, None),
        ([], "alpha,beta", None),
    ],
)
def test_cluster_property_names_one_cluster_or_nothing(
    monkeypatch, sbatch_args, env, expected
):
    """`backend.cluster` is the single cluster a `-M` may name, or None.

    Any per-cluster scheduler query (squeue, sacct, scontrol) reads this,
    so a multi-cluster selection has to resolve to None: qualifying a query
    with one name out of several would ask about a cluster nothing was
    necessarily submitted to.
    """
    if env is not None:
        monkeypatch.setenv("SBATCH_CLUSTERS", env)
    cfg = DispatchConfigFile(sbatch_args=sbatch_args).initialise()
    assert SlurmDispatchBackend(cfg).cluster == expected


# ------------------------------- SchedulerParameters max_array_tasks (#509)


def test_max_array_tasks_caps_the_slice_below_max_array_size(monkeypatch, tmp_path):
    """A cluster may cap tasks-per-array well below MaxArraySize.

    `scontrol show config` keeps reporting the larger MaxArraySize, so
    slicing from that alone hands sbatch an array the cluster refuses —
    and, the limit now being non-None, the failure hint would once have
    been the wrong one (#509 review).
    """
    calls = []
    results = [
        SimpleNamespace(returncode=0, stdout=f"{base}\n", stderr="")
        for base in (100, 101, 102)
    ]
    monkeypatch.setattr(
        slurm_module.subprocess,
        "run",
        _fake_run(calls, results, max_array_size=1001, max_array_tasks=4),
    )
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.submit_array(
        [_spec(run_id=i) for i in range(1, 11)], array_dir=tmp_path / "arr"
    )
    # max_array_tasks is a COUNT (inclusive), not an index bound: 4 means
    # four elements per array, not three.
    assert _array_ranges(calls) == ["1-4", "1-4", "1-2"]


def test_without_max_array_tasks_the_index_bound_still_governs(monkeypatch, tmp_path):
    calls = []
    results = [
        SimpleNamespace(returncode=0, stdout=f"{base}\n", stderr="")
        for base in (100, 101, 102)
    ]
    monkeypatch.setattr(
        slurm_module.subprocess,
        "run",
        _fake_run(calls, results, max_array_size=5),
    )
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    backend.submit_array(
        [_spec(run_id=i) for i in range(1, 11)], array_dir=tmp_path / "arr"
    )
    assert _array_ranges(calls) == ["1-4", "1-4", "1-2"]


def test_the_larger_of_the_two_ceilings_never_wins(monkeypatch, tmp_path, caplog):
    """A max_array_tasks ABOVE the index bound cannot raise the slice."""
    calls = []
    results = [
        SimpleNamespace(returncode=0, stdout=f"{base}\n", stderr="")
        for base in (100, 101, 102)
    ]
    monkeypatch.setattr(
        slurm_module.subprocess,
        "run",
        _fake_run(calls, results, max_array_size=5, max_array_tasks=1000),
    )
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    with caplog.at_level("DEBUG"):
        backend.submit_array(
            [_spec(run_id=i) for i in range(1, 11)], array_dir=tmp_path / "arr"
        )
    assert _array_ranges(calls) == ["1-4", "1-4", "1-2"]
    # Both ceilings are recorded, so a reader can see which one governed.
    (fields,) = _events(caplog, "dispatch.max_array_size")
    assert (fields["max_array_size"], fields["max_array_tasks"]) == (5, 1000)
    assert fields["max_elements"] == 4


def test_max_array_tasks_is_read_from_the_scheduler_parameters_line():
    parse = slurm_module._max_array_tasks
    assert parse("MaxArraySize = 1001\n") is None
    assert parse("SchedulerParameters = bf_window=2880\n") is None
    assert parse("SchedulerParameters = max_array_tasks=64,bf_window=2880\n") == 64
    assert parse("SchedulerParameters = bf_window=2880,max_array_tasks=64\n") == 64
    # A key that merely ENDS in the name is a different parameter.
    assert parse("SchedulerParameters = other_max_array_tasks=64\n") is None
    # ...and a mention outside the SchedulerParameters line is not the setting.
    assert parse("SomeOtherKey = max_array_tasks=64\n") is None
