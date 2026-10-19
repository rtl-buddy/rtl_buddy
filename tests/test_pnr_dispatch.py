"""Tests for `rb pnr --dispatch slurm`: a staged run's stages as chained Slurm jobs.

`_FakeSlurm` answers the real Slurm backend's sbatch, squeue, sacct and scancel calls.
At the head's first queue poll it runs the queued jobs in submission order, each by
calling `run_stage_job` in-process as `rb _pnr-stage-job` does on a compute node, so a
stage runs the stub OpenROAD harness of `test_pnr_checkpoints`. A job whose dependency
did not complete is reaped, as `--kill-on-invalid-dep=yes` makes Slurm do.
"""

import json
import os
import re
import shlex
import signal
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_pnr_checkpoints import _backend, _events, _pnr_cfg, _runs

from rtl_buddy.config.dispatch import (
    DispatchConfigFile,
    DispatchResourcesFile,
    JobResources,
)
from rtl_buddy.config.pnr import PNR_STAGES, PnrConfigFile, PnrResources
from rtl_buddy.dispatch import slurm as slurm_module
from rtl_buddy.dispatch.argv import pnr_stage_job_argv
from rtl_buddy.dispatch.base import JobHandle, PnrStageJobSpec
from rtl_buddy.dispatch.slurm import SlurmDispatchBackend
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.runner.xfail import apply_xfail
from rtl_buddy.tools import pnr_checkpoints, pnr_dispatch

_NAMES = {
    stage: f"{pnr_checkpoints.STAGE_INDEX[stage]}_{stage}" for stage in PNR_STAGES
}


class _JobDied(Exception):
    """The fake scheduler killed a job's whole process tree inside its stage."""


def _done(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _SlurmSubprocess:
    def __init__(self, run):
        self.run = run

    def __getattr__(self, name):
        return getattr(subprocess, name)


class _FakeSlurm:
    """A Slurm that runs each stage job in-process when the head first polls the queue.

    `died` maps a stage to the state of a job killed inside it (no result, no
    `stage_end`); `died_after` to the state of a job killed after its `stage_end`,
    before it wrote its result; `lost` maps a stage to the state of a job that never
    started it; `state_of` overrides the final state of a job that ran.
    """

    def __init__(
        self,
        monkeypatch,
        *,
        died=None,
        died_after=None,
        lost=None,
        state_of=None,
        **cfg,
    ):
        self.monkeypatch = monkeypatch
        self.jobs: dict[str, dict] = {}
        self.cancelled: list[str] = []
        self.polls = 0
        self.sacct_calls = 0
        self.on_poll = None
        self.died = died or {}
        self.died_after = died_after or {}
        self.lost = lost or {}
        self.state_of = state_of or {}
        self._next_id = 501
        monkeypatch.setattr(slurm_module, "require_tool", lambda name: None)
        # The harness patches the shared `subprocess.run` for OpenROAD, so Slurm's
        # module gets a `subprocess` of its own.
        monkeypatch.setattr(slurm_module, "subprocess", _SlurmSubprocess(self.run))
        monkeypatch.setattr(slurm_module.time, "sleep", lambda s: None)
        monkeypatch.setattr(pnr_dispatch.time, "sleep", lambda s: None)
        monkeypatch.delenv("SBATCH_CLUSTERS", raising=False)
        self.backend = SlurmDispatchBackend(DispatchConfigFile(**cfg).initialise())

    def run(self, argv, capture_output=True, text=True, cwd=None, timeout=None):
        argv = list(argv)
        handler = {
            "sbatch": self._sbatch,
            "squeue": self._squeue,
            "sacct": self._sacct,
            "scancel": self._scancel,
        }.get(argv[0])
        if handler is None:
            return _done(1, stderr=f"{argv[0]}: not faked")
        return handler(argv)

    def _sbatch(self, argv):
        flags = {}
        for arg in argv[1:]:
            if arg.startswith("--") and "=" in arg:
                key, value = arg.split("=", 1)
                flags[key] = value
        job_id = str(self._next_id)
        self._next_id += 1
        dependency = flags.get("--dependency")
        self.jobs[job_id] = {
            "id": job_id,
            "argv": argv,
            "wrap": argv[argv.index("--wrap") + 1],
            "dependency": dependency.split(":", 1)[1] if dependency else None,
            "flags": flags,
            "state": None,
        }
        return _done(stdout=f"{job_id}\n")

    def _squeue(self, argv):
        self.polls += 1
        if self.on_poll is not None:
            hook, self.on_poll = self.on_poll, None
            hook()
        for job in self.jobs.values():
            if job["state"] is not None:
                continue
            dependency = job["dependency"]
            if dependency is not None and self.jobs[dependency]["state"] != "COMPLETED":
                job["state"] = "CANCELLED"
                continue
            job["state"] = self._execute(job)
        return _done()

    def _execute(self, job) -> str:
        argv = shlex.split(job["wrap"])
        tail = argv[argv.index("_pnr-stage-job") + 1 :]
        opts = dict(zip(tail[::2], tail[1::2]))
        stage = opts["--stage"]
        job["stage"] = stage
        if stage in self.lost:
            return self.lost[stage]
        real_end = pnr_checkpoints.stage_end
        real_write = pnr_dispatch._write_result

        def _killed(*_a, **_k):
            raise _JobDied

        if stage in self.died:
            pnr_checkpoints.stage_end = _killed
        if stage in self.died_after:
            pnr_dispatch._write_result = _killed
        os.environ["SLURM_JOB_ID"] = job["id"]
        try:
            ok = pnr_dispatch.run_stage_job(
                opts["--run-dir"],
                stage,
                openroad=opts["--openroad"],
                pnr_log=opts["--log"],
            )
        except _JobDied:
            return {**self.died, **self.died_after}[stage]
        finally:
            pnr_checkpoints.stage_end = real_end
            pnr_dispatch._write_result = real_write
            del os.environ["SLURM_JOB_ID"]
        return self.state_of.get(stage, "COMPLETED" if ok else "FAILED")

    def _sacct(self, argv):
        self.sacct_calls += 1
        ids = argv[argv.index("--jobs") + 1].split(",")
        lines = []
        for job_id in ids:
            job = self.jobs[job_id]
            state = job["state"] or "CANCELLED"
            lagging = job.get("accounting_lag", 0)
            if lagging:
                # Accounting still has the job's last live state.
                job["accounting_lag"] = lagging - 1
                state = "RUNNING"
            lines.append(f"{job_id}|{state}|42|60|8|8|4G||")
            lines.append(f"{job_id}.batch|{state}|42||8|8||01:30.000|2048000K")
        return _done(stdout="\n".join(lines) + "\n")

    def _scancel(self, argv):
        ids = [a for a in argv[1:] if not a.startswith("-")]
        self.cancelled.extend(ids)
        for job_id in ids:
            if self.jobs[job_id]["state"] is None:
                self.jobs[job_id]["state"] = "CANCELLED"
        return _done()

    def submitted(self) -> list[dict]:
        return list(self.jobs.values())


#: `cfg-dispatch.resources` of every dispatched run here.
CFG_RESOURCES = DispatchResourcesFile(cpus=2, mem="4G", time="00:30:00")


def _dispatched(tmp_path, monkeypatch, *, slurm=None, resources=None, cfg=None, **kw):
    """A dispatched `OpenRoadPnr` on the stub harness, its artefact dir and the fake Slurm."""
    slurm = slurm or _FakeSlurm(monkeypatch)
    cfg = cfg or _pnr_cfg(tmp_path, stages=True)
    if resources is not None:
        cfg = replace(cfg, resources=resources)
    backend, artefacts = _backend(
        tmp_path, monkeypatch, pnr_cfg=cfg, dispatch=slurm.backend, **kw
    )
    backend.root_cfg.get_dispatch_cfg.return_value = DispatchConfigFile(
        resources=CFG_RESOURCES
    ).initialise()
    return backend, artefacts, slurm


def _only_run(artefacts: Path) -> Path:
    (run_dir,) = _runs(artefacts)
    return run_dir


def _stage_of(job) -> str:
    argv = shlex.split(job["wrap"])
    return argv[argv.index("--stage") + 1]


def test_a_dispatched_run_submits_one_chained_job_per_stage(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["routed"] is True
    run_dir = _only_run(artefacts)
    jobs = slurm.submitted()
    assert [_stage_of(j) for j in jobs] == list(PNR_STAGES)
    assert [j["state"] for j in jobs] == ["COMPLETED"] * len(PNR_STAGES)
    assert jobs[0]["dependency"] is None
    assert "--kill-on-invalid-dep=yes" not in jobs[0]["argv"]
    for before, job in zip(jobs, jobs[1:]):
        assert job["dependency"] == before["id"]
        assert "--kill-on-invalid-dep=yes" in job["argv"]
    for stage, job in zip(PNR_STAGES, jobs):
        flags = job["flags"]
        assert flags["--job-name"] == f"rb:pnr:demo_pnr:{stage}"
        assert flags["--chdir"] == str(tmp_path)
        assert flags["--output"] == pnr_dispatch.job_file(
            str(run_dir), stage, "slurm.log"
        )
        argv = shlex.split(job["wrap"])
        assert argv[argv.index("--run-dir") + 1] == str(run_dir)
        assert argv[argv.index("--log") + 1] == str(artefacts / "pnr.log")
    # The head writes each stage's job as Slurm accepts it.
    recorded = json.loads((run_dir / "dispatch" / "jobs.json").read_text())
    assert recorded["backend"] == "slurm"
    assert [j["job_id"] for j in recorded["jobs"]] == [j["id"] for j in jobs]
    assert [j["dependency"] for j in recorded["jobs"]] == [None] + [
        j["id"] for j in jobs[:-1]
    ]
    # Each job measured its own stage and named itself in the stage's events.
    ended = [e for e in _events(run_dir) if e["event"] == "stage_end"]
    assert [e["stage"] for e in ended] == list(PNR_STAGES)
    assert [e["slurm_job"] for e in ended] == [j["id"] for j in jobs]
    assert all(e["measured_by"] == "process" for e in ended)
    assert all(e["status"] == "ok" for e in ended)
    assert all(e["threads"] == 1 for e in ended)
    for stage in PNR_STAGES:
        result = pnr_dispatch.read_result(str(run_dir), stage)
        assert result["ok"] is True and result["started"] is True
    # pnr.log is the stage logs in order, as in a local staged run.
    assert (artefacts / "pnr.log").read_bytes() == b"".join(
        (run_dir / f"{_NAMES[stage]}.log").read_bytes() for stage in PNR_STAGES
    )
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["staged"] is True
    assert manifest["dispatch"]["backend"] == "slurm"
    assert list(manifest["dispatch"]["stages"]) == list(PNR_STAGES)
    assert list(manifest["resources"]) == list(PNR_STAGES)
    assert manifest["outcome"]["result"] == "PASS"


def test_dispatch_makes_a_run_staged_whatever_its_config_says(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(
        tmp_path, monkeypatch, cfg=_pnr_cfg(tmp_path, stages=False)
    )

    assert backend.run().results["result"] == "PASS"
    assert len(slurm.submitted()) == len(PNR_STAGES)


def test_stage_reservations_layer_field_by_field(tmp_path, monkeypatch):
    resources = PnrResources(
        base=DispatchResourcesFile(cpus=4, time="01:00:00"),
        stages={"detailed_route": DispatchResourcesFile(cpus=16, mem="32G")},
    )
    cfg = replace(_pnr_cfg(tmp_path, stages=True), threads="auto")
    backend, artefacts, slurm = _dispatched(
        tmp_path, monkeypatch, cfg=cfg, resources=resources
    )

    assert backend.run().results["result"] == "PASS"

    by_stage = {_stage_of(j): j["flags"] for j in slurm.submitted()}
    for stage, flags in by_stage.items():
        if stage == "detailed_route":
            # The stage's own block, then the run's, then cfg-dispatch.
            assert flags["--cpus-per-task"] == "16"
            assert flags["--mem"] == "32G"
        else:
            assert flags["--cpus-per-task"] == "4"
            assert flags["--mem"] == "4G"
        assert flags["--time"] == "01:00:00"
    run_dir = _only_run(artefacts)
    # `threads: auto` takes each job's CPU count.
    for stage in PNR_STAGES:
        script = (run_dir / f"{_NAMES[stage]}.tcl").read_text()
        count = 16 if stage == "detailed_route" else 4
        assert f"set_thread_count {count}\n" in script
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["dispatch"]["stages"]["detailed_route"] == {
        "cpus": 16,
        "mem": "32G",
        "time": "01:00:00",
        "threads": 16,
    }
    assert manifest["dispatch"]["stages"]["cts"]["threads"] == 4
    ended = {e["stage"]: e for e in _events(run_dir) if e["event"] == "stage_end"}
    assert ended["detailed_route"]["threads"] == 16
    assert ended["cts"]["threads"] == 4


def test_a_fixed_thread_count_is_the_same_in_every_stage(tmp_path, monkeypatch):
    resources = PnrResources(
        stages={"detailed_route": DispatchResourcesFile(cpus=8)},
    )
    cfg = replace(_pnr_cfg(tmp_path, stages=True), threads=2)
    backend, artefacts, _ = _dispatched(
        tmp_path, monkeypatch, cfg=cfg, resources=resources
    )

    assert backend.run().results["result"] == "PASS"

    run_dir = _only_run(artefacts)
    for stage in PNR_STAGES:
        assert "set_thread_count 2\n" in (run_dir / f"{_NAMES[stage]}.tcl").read_text()


def _comparable(results: dict, root: Path) -> dict:
    # Thread provenance names the allocation each session ran in: the head's here,
    # the last stage job's when dispatched.
    results = {k: v for k, v in results.items() if k != "openroad_threads"}
    text = json.dumps(results, sort_keys=True, default=str)
    text = text.replace(str(root), "<root>")
    text = re.sub(r"\d{8}T\d{6}-\d+", "<run>", text)
    return json.loads(text)


@pytest.mark.parametrize(
    "env",
    [
        None,
        {"RB_STUB_FAIL": "global_route"},
        # Exits 0 before its end-of-stage line: `fail_stage: incomplete`.
        {"RB_STUB_EXIT": "check_placement"},
        # Its checkpoint cannot be written: `fail_stage: checkpoint`.
        {"RB_STUB_FAIL": "write_db"},
        # An `[ERROR` line in a session that exits 0.
        {"RB_STUB_ERRLINE": "check_placement"},
    ],
)
def test_the_result_is_the_same_as_the_run_staged_locally(tmp_path, monkeypatch, env):
    local_root, dispatched_root = tmp_path / "local", tmp_path / "dispatched"
    local_root.mkdir()
    dispatched_root.mkdir()
    local, _ = _backend(local_root, monkeypatch, stages=True, env=env)
    local_res = local.run()
    dispatched, _, _ = _dispatched(dispatched_root, monkeypatch, env=env)
    dispatched_res = dispatched.run()

    expected = _comparable(local_res.results, local_root)
    got = _comparable(dispatched_res.results, dispatched_root)
    if env is None:
        assert got == expected
    else:
        assert got["result"] == expected["result"] == "FAIL"
        assert got.get("fail_stage") == expected.get("fail_stage")
        # The dispatched description adds the scheduler's account of the job.
        assert got["desc"].startswith(expected["desc"])
        assert re.search(r"\(Slurm job \d+ ended FAILED\)$", got["desc"])
        assert {k: v for k, v in got.items() if k != "desc"} == {
            k: v for k, v in expected.items() if k != "desc"
        }


def test_the_description_keeps_the_first_stderr_line_the_job_cut(tmp_path, monkeypatch):
    env = {"RB_STUB_FAIL": "global_route"}
    local_root, dispatched_root = tmp_path / "local", tmp_path / "dispatched"
    local_root.mkdir()
    dispatched_root.mkdir()
    local, _ = _backend(local_root, monkeypatch, stages=True, env=env)
    expected = local.run().results["desc"]
    monkeypatch.setattr(pnr_dispatch, "_STDERR_KEEP", 12)
    dispatched, artefacts, _ = _dispatched(dispatched_root, monkeypatch, env=env)

    got = dispatched.run().results["desc"]

    result = pnr_dispatch.read_result(str(_only_run(artefacts)), "global_route")
    assert "STUB-0001" not in result["stderr"]
    assert "STUB-0001" in expected
    assert got.startswith(expected + " (Slurm job ")


def test_a_failed_middle_stage_fails_the_run_and_reaps_the_rest(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(
        tmp_path, monkeypatch, env={"RB_STUB_FAIL": "global_route"}
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    jobs = {_stage_of(j): j for j in slurm.submitted()}
    failed = jobs["global_route"]["id"]
    assert res.results["desc"].startswith("stage 04_global_route: OpenROAD exited")
    assert res.results["desc"].endswith(f"(Slurm job {failed} ended FAILED)")
    assert [jobs[s]["state"] for s in ("detailed_route", "finish")] == [
        "CANCELLED",
        "CANCELLED",
    ]
    run_dir = _only_run(artefacts)
    assert not (run_dir / "05_detailed_route.log").exists()
    ended = {e["stage"]: e for e in _events(run_dir) if e["event"] == "stage_end"}
    assert list(ended) == ["floorplan", "place", "cts", "global_route"]
    assert ended["global_route"]["status"] == "error"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["outcome"]["result"] == "FAIL"
    assert not list(artefacts.glob("*.routed.*"))


def test_openroad_killed_for_memory_names_the_stage_and_the_state(
    tmp_path, monkeypatch
):
    slurm = _FakeSlurm(monkeypatch, state_of={"detailed_route": "OUT_OF_MEMORY"})
    backend, artefacts, _ = _dispatched(
        tmp_path, monkeypatch, slurm=slurm, env={"RB_STUB_KILL": "detailed_route"}
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    job = next(j for j in slurm.submitted() if _stage_of(j) == "detailed_route")
    assert re.fullmatch(
        rf"stage 05_detailed_route: OpenROAD exited with code -9.*"
        rf"\(Slurm job {job['id']} ended OUT_OF_MEMORY\)",
        res.results["desc"],
    ), res.results["desc"]
    # The scheduler ended the job, so the stage's exit is no verdict on the design.
    assert res.results["fail_stage"] == "dispatch"
    apply_xfail(res)
    assert res.results["result"] == "FAIL"


def test_a_job_killed_inside_its_stage_is_closed_from_accounting(tmp_path, monkeypatch):
    slurm = _FakeSlurm(monkeypatch, died={"detailed_route": "TIMEOUT"})
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)

    res = backend.run()

    job = next(j for j in slurm.submitted() if _stage_of(j) == "detailed_route")
    assert res.results["result"] == "FAIL"
    assert res.results["desc"] == (
        f"stage 05_detailed_route: Slurm job {job['id']} ended TIMEOUT during the stage"
    )
    run_dir = _only_run(artefacts)
    assert (run_dir / "05_detailed_route.log").is_file()
    ended = {e["stage"]: e for e in _events(run_dir) if e["event"] == "stage_end"}
    assert res.results["fail_stage"] == "dispatch"
    killed = ended["detailed_route"]
    assert killed["status"] == "error"
    assert killed["measured_by"] == "scheduler"
    assert killed["slurm_state"] == "TIMEOUT"
    assert killed["slurm_job"] == job["id"]
    assert killed["wall_s"] == 42 and killed["cpu_s"] == 90.0
    assert killed["peak_rss_kb"] == 2048000
    assert "finish" not in ended


@pytest.mark.parametrize("state", ["NODE_FAIL", "CANCELLED", "TIMEOUT", "FAILED"])
def test_a_job_that_never_started_its_stage_points_at_its_scheduler_log(
    tmp_path, monkeypatch, state
):
    slurm = _FakeSlurm(monkeypatch, lost={"cts": state})
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)

    res = backend.run()

    run_dir = _only_run(artefacts)
    job = next(j for j in slurm.submitted() if _stage_of(j) == "cts")
    log = pnr_dispatch.job_file(str(run_dir), "cts", "slurm.log")
    assert res.results["result"] == "FAIL"
    assert res.results["desc"] == (
        f"stage 03_cts: Slurm job {job['id']} ended {state} before the stage "
        f"started (see {log})"
    )
    assert res.results["fail_stage"] == "dispatch"
    apply_xfail(res)
    assert res.results["result"] == "FAIL"
    assert res.results["desc"].startswith("xfail not applied (dispatch failure): ")


def test_accounting_that_still_reads_running_is_asked_again(tmp_path, monkeypatch):
    slurm = _FakeSlurm(monkeypatch, died={"cts": "TIMEOUT"})
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)
    real_squeue = slurm._squeue

    def _squeue(argv):
        done = real_squeue(argv)
        for job in slurm.jobs.values():
            job.setdefault("accounting_lag", 2)
        return done

    slurm._squeue = _squeue

    res = backend.run()

    job = next(j for j in slurm.submitted() if _stage_of(j) == "cts")
    assert res.results["desc"] == (
        f"stage 03_cts: Slurm job {job['id']} ended TIMEOUT during the stage"
    )
    assert slurm.sacct_calls == 3


def test_a_job_that_ended_its_stage_without_a_result_says_so(tmp_path, monkeypatch):
    slurm = _FakeSlurm(monkeypatch, died_after={"cts": "TIMEOUT"})
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)

    res = backend.run()

    run_dir = _only_run(artefacts)
    job = next(j for j in slurm.submitted() if _stage_of(j) == "cts")
    log = pnr_dispatch.job_file(str(run_dir), "cts", "slurm.log")
    assert res.results["desc"] == (
        f"stage 03_cts: Slurm job {job['id']} ended TIMEOUT after the stage ended, "
        f"without writing its result (see {log})"
    )
    assert res.results["fail_stage"] == "dispatch"


def test_a_refused_stage_is_a_dispatch_failure(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    real = pnr_dispatch._refusal
    monkeypatch.setattr(
        pnr_dispatch,
        "_refusal",
        lambda run_dir, stage: (
            "the stage is refused" if stage == "place" else real(run_dir, stage)
        ),
    )

    res = backend.run()

    job = next(j for j in slurm.submitted() if _stage_of(j) == "place")
    assert res.results["desc"] == (
        f"stage 02_place: the stage is refused (Slurm job {job['id']} ended FAILED)"
    )
    assert res.results["fail_stage"] == "dispatch"
    apply_xfail(res)
    assert res.results["result"] == "FAIL"


@pytest.mark.parametrize(
    ("signum", "raised"),
    [(signal.SIGINT, KeyboardInterrupt), (signal.SIGTERM, SystemExit)],
)
def test_an_interrupt_cancels_this_runs_jobs_and_no_others(
    tmp_path, monkeypatch, signum, raised
):
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    slurm.jobs["9001"] = {"id": "9001", "state": "RUNNING", "dependency": None}
    slurm.on_poll = lambda: os.kill(os.getpid(), signum)

    with pytest.raises(raised) as caught:
        with pnr_dispatch.cancel_on_interrupt():
            backend.run()

    if raised is SystemExit:
        assert caught.value.code == 128 + signum
    ours = [j["id"] for j in slurm.submitted() if j["id"] != "9001"]
    assert len(ours) == len(PNR_STAGES)
    assert sorted(slurm.cancelled) == sorted(ours)
    assert slurm.jobs["9001"]["state"] == "RUNNING"
    run_dir = _only_run(artefacts)
    assert not [e for e in _events(run_dir) if e["event"] == "stage_begin"]
    # The handlers are restored.
    assert getattr(signal.getsignal(signum), "__name__", None) != "_handler"


def test_a_failed_wait_cancels_the_chain_and_fails_the_run(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)

    def _give_up():
        raise FatalRtlBuddyError("queue drain exceeded max-wait")

    slurm.on_poll = _give_up

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "dispatch"
    assert res.results["desc"] == "dispatch: queue drain exceeded max-wait"
    assert sorted(slurm.cancelled) == sorted(j["id"] for j in slurm.submitted())


def test_a_dispatch_failure_is_prefixed_once():
    assert pnr_dispatch.failure_desc("sbatch failed") == "dispatch: sbatch failed"
    assert (
        pnr_dispatch.failure_desc(FatalRtlBuddyError("dispatch: 1 of 6 job(s)"))
        == "dispatch: 1 of 6 job(s)"
    )


def test_a_chain_cancelled_during_a_submission_cancels_the_accepted_job():
    cancelled = []

    class _Backend:
        name = "slurm"

        def submit_pnr_stage(self, spec, *, dependency=None):
            handle = JobHandle(job_id=str(len(chain.handles) + 1), spec=spec)
            if spec.stage == "place":
                chain.cancel()
            return handle

        def cancel_all(self, handles):
            cancelled.append([h.job_id for h in handles])

    chain = pnr_dispatch.StageChain(_Backend())
    spec = SimpleNamespace(stage="floorplan")
    with pytest.raises(pnr_dispatch.ChainCancelled):
        with chain:
            chain.submit(spec)
            chain.submit(SimpleNamespace(stage="place"))
            chain.submit(SimpleNamespace(stage="cts"))
    assert cancelled == [["1"], ["2"]]
    assert [h.job_id for h in chain.handles] == ["1", "2"]


def test_stop_after_dispatches_only_the_stages_it_runs(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(
        tmp_path, monkeypatch, stop_after="global_route"
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["routed"] is False
    assert res.results["stopped_after"] == "global_route"
    assert [_stage_of(j) for j in slurm.submitted()] == list(PNR_STAGES[:4])


def test_a_resumed_dispatch_submits_only_the_stages_it_runs(tmp_path, monkeypatch):
    first, artefacts = _backend(
        tmp_path, monkeypatch, stages=True, stop_after="global_route"
    )
    assert first.run().results["result"] == "PASS"
    (first_dir,) = _runs(artefacts)

    backend, _, slurm = _dispatched(
        tmp_path, monkeypatch, resume=first_dir.name, resume_from="detailed_route"
    )
    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["routed"] is True
    jobs = slurm.submitted()
    assert [_stage_of(j) for j in jobs] == ["detailed_route", "finish"]
    assert jobs[0]["dependency"] is None
    assert jobs[1]["dependency"] == jobs[0]["id"]
    (run_dir,) = [r for r in _runs(artefacts) if r != first_dir]
    assert (
        (artefacts / "pnr.log")
        .read_bytes()
        .startswith((first_dir / "01_floorplan.log").read_bytes())
    )
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["resumed_from"]["run_id"] == first_dir.name
    assert list(manifest["dispatch"]["stages"]) == ["detailed_route", "finish"]
    assert list(manifest["resources"]) == ["detailed_route", "finish"]


def test_a_stage_job_runs_only_for_the_latest_unfinished_run(tmp_path, monkeypatch):
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch)
    assert backend.run().results["result"] == "PASS"
    run_dir = str(_only_run(artefacts))

    # The run has finished, so a late job of its chain does nothing.
    assert not pnr_dispatch.run_stage_job(
        run_dir, "finish", openroad="openroad", pnr_log=str(artefacts / "pnr.log")
    )
    result = pnr_dispatch.read_result(run_dir, "finish")
    assert result["started"] is False
    assert "already finished" in result["problem"]

    # A later run of the same P&R run has moved `latest`.
    newer = Path(artefacts, "checkpoints", "20991231T000000-1")
    newer.mkdir()
    latest = Path(artefacts, "checkpoints", pnr_checkpoints.LATEST_NAME)
    latest.unlink()
    latest.symlink_to(newer.name)
    assert pnr_dispatch._refusal(run_dir, "finish").endswith(
        "a later rb pnr of the run has started"
    )


def test_submit_pnr_stage_argv(monkeypatch):
    slurm = _FakeSlurm(
        monkeypatch,
        sbatch_args=["--partition=pd", "--kill-on-invalid-dep=no", "--dependency=x:1"],
    )
    monkeypatch.setenv("RTL_BUDDY_JOB_TAG", "nightly")
    spec = PnrStageJobSpec(
        suite_dir="/proj/pnr/blk",
        run_name="blk",
        run_dir="/proj/pnr/blk/artefacts/blk/checkpoints/r1",
        stage="cts",
        openroad="/opt/openroad/bin/openroad",
        pnr_log="/proj/pnr/blk/artefacts/blk/pnr.log",
        resources=JobResources(cpus=8, mem="16G", time="02:00:00"),
        log_path=Path("/proj/pnr/blk/artefacts/blk/checkpoints/r1/dispatch/x.log"),
    )

    handle = slurm.backend.submit_pnr_stage(spec, dependency="41")

    argv = slurm.jobs[handle.job_id]["argv"]
    assert "--cpus-per-task=8" in argv and "--mem=16G" in argv
    assert "--time=02:00:00" in argv
    assert "--chdir=/proj/pnr/blk" in argv
    assert argv.index("--ntasks=1") < argv.index("--partition=pd")
    assert any(a.startswith("--job-name=rb:pnr:blk:cts") for a in argv)
    # The chain's dependency wins over one in sbatch-args; the site still owns reaping.
    assert argv.index("--dependency=afterok:41") > argv.index("--dependency=x:1")
    assert argv.index("--kill-on-invalid-dep=yes") < argv.index(
        "--kill-on-invalid-dep=no"
    )
    assert shlex.split(argv[-1]) == pnr_stage_job_argv(spec)


def test_rb_pnr_stage_job_runs_the_stage_its_argv_names(minimal_project, monkeypatch):
    from typer.testing import CliRunner

    from rtl_buddy.rtl_buddy import RtlBuddy

    run_dir = minimal_project / "artefacts" / "r" / "checkpoints" / "r1"
    (run_dir / "dispatch").mkdir(parents=True)
    spec = PnrStageJobSpec(
        suite_dir=str(minimal_project),
        run_name="r",
        run_dir=str(run_dir),
        stage="detailed_route",
        openroad="/opt/openroad/bin/openroad",
        pnr_log=str(minimal_project / "artefacts" / "r" / "pnr.log"),
        resources=JobResources(),
        log_path=run_dir / "dispatch" / "05_detailed_route.slurm.log",
    )
    calls = []

    def _job(run_dir, stage, *, openroad, pnr_log):
        calls.append((run_dir, stage, openroad, pnr_log))
        return stage == "detailed_route"

    monkeypatch.setattr(pnr_dispatch, "run_stage_job", _job)
    argv = pnr_stage_job_argv(spec)
    result = CliRunner().invoke(
        RtlBuddy(name="test_pnr_stage_job").app, argv[argv.index("--machine") :]
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (str(run_dir), "detailed_route", spec.openroad, spec.pnr_log),
    ]
    assert json.loads(result.output.splitlines()[-1])["exit_code"] == 0
    assert (run_dir / "dispatch" / "05_detailed_route.rtl_buddy.log").is_file()

    failing = argv[argv.index("--machine") :]
    failing[failing.index("--stage") + 1] = "finish"
    result = CliRunner().invoke(RtlBuddy(name="test_pnr_stage_job").app, failing)
    assert result.exit_code == 1


def test_rb_pnr_dispatch_selects_slurm_only(monkeypatch):
    from rtl_buddy import rtl_buddy as rtl_buddy_module
    from rtl_buddy.rtl_buddy import RtlBuddy

    made = []
    monkeypatch.setattr(
        rtl_buddy_module,
        "create_dispatch_backend",
        lambda name, cfg, *, config_path=None: made.append(name) or "backend",
    )
    rb = RtlBuddy.__new__(RtlBuddy)
    rb.root_cfg = SimpleNamespace(get_dispatch_cfg=lambda: None)

    assert rb._pnr_dispatch_backend(None) is None
    assert rb._pnr_dispatch_backend("local") is None
    assert rb._pnr_dispatch_backend("slurm") == "backend"
    assert made == ["slurm"]
    with pytest.raises(FatalRtlBuddyError, match="local-parallel has nothing"):
        rb._pnr_dispatch_backend("local-parallel")
    with pytest.raises(FatalRtlBuddyError, match="bogus"):
        rb._pnr_dispatch_backend("bogus")


def test_the_dispatch_backend_reaches_the_named_run(tmp_path, monkeypatch):
    from test_pnr_plan import _Locks, _run, _suite

    from rtl_buddy.rtl_buddy import RtlBuddy
    from rtl_buddy.runner.pnr_results import PnrPassResults

    seen = {}

    class _Runner:
        def __init__(self, *, name, **kw):
            seen[name] = kw.get("dispatch")
            self.name = name

        def run(self):
            return PnrPassResults(name=f"{self.name}/results")

    monkeypatch.setattr("rtl_buddy.rtl_buddy.PnrRunner", _Runner)
    rb = RtlBuddy.__new__(RtlBuddy)
    rb.root_cfg = None
    rb._artifact_locks = _Locks()
    suite = _suite(tmp_path / "pnr.yaml", _run("a"), _run("b"))

    rb._do_pnr_suite(suite, dispatch="the-backend")

    assert seen == {"a": "the-backend", "b": "the-backend"}


def _cfg_file(resources):
    return PnrConfigFile(
        name="r",
        desc="r",
        synth="s",
        synth_path="synth.yaml",
        platform="p",
        resources=resources,
    )


def test_resources_block_loads_with_stage_overrides(tmp_path):
    from rtl_buddy.config.pnr import PnrResourcesFile

    cfg = _cfg_file(
        PnrResourcesFile(
            cpus=4,
            time="02:00:00",
            stages={"detailed_route": {"cpus": 16, "mem": "64G"}},
        )
    ).initialise(str(tmp_path))
    dispatch_cfg = DispatchConfigFile(resources=CFG_RESOURCES).initialise()

    assert cfg.stage_resources(dispatch_cfg, "place") == JobResources(
        cpus=4, mem="4G", time="02:00:00"
    )
    assert cfg.stage_resources(dispatch_cfg, "detailed_route") == JobResources(
        cpus=16, mem="64G", time="02:00:00"
    )
    assert cfg.stage_resources(None, "place").cpus == 4


@pytest.mark.parametrize(
    ("resources", "expected"),
    [
        ({"stages": {"routing": {"cpus": 2}}}, "routing"),
        ({"stages": {"cts": {"cpus": 0}}}, "cpus"),
        ({"stages": {"cts": 4}}, "cts"),
        ({"time": "4:00:00:00:00"}, "time"),
    ],
)
def test_resources_block_rejects_bad_values(tmp_path, resources, expected):
    from rtl_buddy.config.pnr import PnrResourcesFile

    with pytest.raises(FatalRtlBuddyError, match=expected):
        _cfg_file(PnrResourcesFile(**resources)).initialise(str(tmp_path))
