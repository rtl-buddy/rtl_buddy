"""Tests for `rb pnr --dispatch slurm`: a staged run's stages as chained Slurm jobs.

`_FakeSlurm` answers the real Slurm backend's sbatch, squeue, sacct and scancel calls.
At the head's first queue poll it runs the queued jobs in submission order, each by
calling `run_stage_job` in-process as `rb _pnr-stage-job` does on a compute node, so a
stage runs the stub OpenROAD harness of `test_pnr_checkpoints`. A job whose dependency
did not complete is reaped, as `--kill-on-invalid-dep=yes` makes Slurm do.
"""

import json
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_pnr_checkpoints import _backend, _events, _pnr_cfg, _runs

from rtl_buddy.config import openroad_threads
from rtl_buddy.config.dispatch import (
    DispatchConfigFile,
    DispatchResourcesFile,
    JobResources,
)
from rtl_buddy.config.pnr import PNR_STAGES, PnrConfigFile, PnrResources
from rtl_buddy.dispatch import slurm as slurm_module
from rtl_buddy.dispatch.argv import pnr_stage_job_argv
from rtl_buddy.dispatch.base import (
    CANCEL_TIMEOUT_S,
    JobHandle,
    PnrStageJobSpec,
    SubmissionAbandoned,
)
from rtl_buddy.dispatch.slurm import SlurmDispatchBackend
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy import logging_utils
from rtl_buddy.runner.xfail import apply_xfail
from rtl_buddy.tools import pnr_checkpoints, pnr_dispatch, pnr_openroad, pnr_stages

#: A run without post-route repair.
_STAGES = pnr_stages.TEMPLATE_STAGES
_NAMES = {
    stage: f"{pnr_checkpoints.STAGE_INDEX[stage]}_{stage}" for stage in PNR_STAGES
}


class _JobDied(Exception):
    """The fake scheduler killed a job's whole process tree inside its stage."""


def _done(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _SlurmSubprocess:
    def __init__(self, slurm):
        self.run = slurm.run
        self.Popen = slurm.popen

    def __getattr__(self, name):
        return getattr(subprocess, name)


class _GuardedSbatch:
    """`_GUARDED_SBATCH` as the fake runs it: sbatch answers after `waits` polls, and a submission given up by then has its job cancelled, as the script does."""

    def __init__(self, slurm, argv):
        assert argv[:2] == ["/bin/sh", "-c"] and argv[3] == "rb-sbatch"
        self.slurm = slurm
        self.answer, self.marker, self.cluster = argv[4], argv[5], argv[6]
        # sbatch runs under `timeout`, so the script files an answer in bounded time.
        assert argv[7:11] == [
            "timeout",
            "-k",
            "10",
            f"{slurm_module.SUBMIT_TIMEOUT_S:g}",
        ]
        self.cmd = argv[11:]
        self.waits = slurm.sbatch_waits
        self.returncode = None

    def wait(self, timeout=None):
        if self.waits > 0:
            self.waits -= 1
            if self.slurm.on_sbatch_wait is not None:
                self.slurm.on_sbatch_wait(self)
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return self.finish()

    def finish(self):
        """sbatch answers."""
        if self.returncode is None:
            done = self.slurm.run(self.cmd)
            Path(self.answer).write_text(done.stdout)
            Path(f"{self.answer}.err").write_text(done.stderr)
            self.returncode = done.returncode
            job_id = done.stdout.strip().split(";")[0]
            if os.path.exists(self.marker) and job_id:
                self.slurm.run(["scancel", job_id])
        return self.returncode


class _FakeSlurm:
    """A Slurm that runs each stage job in-process when the head first polls the queue.

    `died` maps a stage to the state of a job killed inside it (no result, no
    `stage_end`); `died_in_session` to the state of one killed while OpenROAD ran,
    before the stage's log was written; `died_after` to the state of a job killed
    after its `stage_end`, before it wrote its result; `lost` maps a stage to the
    state of a job that never started it; `state_of` overrides the final state of a
    job that ran. `interrupt_in` maps a stage to a signal the head receives while
    the stage's session runs; the job then stays running until it is cancelled.

    Once `hold` is set, a poll runs nothing more and lists the jobs still queued.
    `sbatch_waits` polls of a submission pass before sbatch answers, each calling
    `on_sbatch_wait(guard)`.
    """

    def __init__(
        self,
        monkeypatch,
        *,
        died=None,
        died_in_session=None,
        died_after=None,
        lost=None,
        state_of=None,
        interrupt_in=None,
        **cfg,
    ):
        self.monkeypatch = monkeypatch
        self.jobs: dict[str, dict] = {}
        self.cancelled: list[str] = []
        self.polls = 0
        self.sacct_calls = 0
        self.on_poll = None
        self.died = died or {}
        self.died_in_session = died_in_session or {}
        self.died_after = died_after or {}
        self.interrupt_in = interrupt_in or {}
        self.hold = False
        self.sbatch_waits = 0
        self.on_sbatch_wait = None
        self.on_scancel = None
        self.scancel_rc = []
        self.guards: list[_GuardedSbatch] = []
        self.lost = lost or {}
        self.state_of = state_of or {}
        #: `(argv, timeout)` of each scheduler command.
        self.calls: list[tuple[list, float | None]] = []
        self.sacct_rc = 0
        self._next_id = 501
        monkeypatch.setattr(slurm_module, "require_tool", lambda name: None)
        # The harness patches the shared `subprocess.run` for OpenROAD, so Slurm's
        # module gets a `subprocess` of its own.
        monkeypatch.setattr(slurm_module, "subprocess", _SlurmSubprocess(self))
        monkeypatch.setattr(slurm_module.time, "sleep", lambda s: None)
        monkeypatch.setattr(pnr_dispatch.time, "sleep", lambda s: None)
        monkeypatch.setattr(pnr_dispatch, "_pause", lambda wake, s: None)
        # A job's allocation is what the fake gives it, whatever this host's affinity.
        monkeypatch.setattr(
            pnr_dispatch,
            "detect_allocation",
            lambda: openroad_threads.detect_allocation(dict(os.environ)),
        )
        monkeypatch.delenv("SBATCH_CLUSTERS", raising=False)
        self.backend = SlurmDispatchBackend(DispatchConfigFile(**cfg).initialise())

    def popen(self, argv, **kwargs):
        guard = _GuardedSbatch(self, list(argv))
        self.guards.append(guard)
        return guard

    def run(self, argv, capture_output=True, text=True, cwd=None, timeout=None):
        argv = list(argv)
        self.calls.append((argv, timeout))
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
        if self.hold:
            asked = argv[argv.index("--jobs") + 1].split(",")
            lines = [
                f"{job_id}|None|{self.jobs[job_id]['state'] or 'PENDING'}|0:00|rb"
                for job_id in asked
                if self.jobs[job_id]["state"] in (None, "RUNNING")
            ]
            return _done(stdout="".join(f"{line}\n" for line in lines))
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
        real_session = pnr_openroad._run_session

        def _killed(*_a, **_k):
            raise _JobDied

        def _cut_session(cmd, *, stdout, env, cwd):
            stdout.write(b"stub: part of the stage before the job ended\n")
            stdout.flush()
            if stage in self.died_in_session:
                raise _JobDied
            job["state"] = "RUNNING"
            self.hold = True
            os.kill(os.getpid(), self.interrupt_in[stage])
            raise AssertionError("the interrupt did not reach the head")

        if stage in self.died:
            pnr_checkpoints.stage_end = _killed
        if stage in self.died_after:
            pnr_dispatch._write_result = _killed
        if stage in self.died_in_session or stage in self.interrupt_in:
            pnr_openroad._run_session = _cut_session
        os.environ["SLURM_JOB_ID"] = job["id"]
        if "--cpus-per-task" in job["flags"]:
            os.environ["SLURM_CPUS_PER_TASK"] = job["flags"]["--cpus-per-task"]
        try:
            ok = pnr_dispatch.run_stage_job(
                opts["--run-dir"],
                stage,
                openroad=opts["--openroad"],
                pnr_log=opts["--log"],
            )
        except _JobDied:
            return {**self.died, **self.died_in_session, **self.died_after}[stage]
        finally:
            pnr_checkpoints.stage_end = real_end
            pnr_dispatch._write_result = real_write
            pnr_openroad._run_session = real_session
            del os.environ["SLURM_JOB_ID"]
            os.environ.pop("SLURM_CPUS_PER_TASK", None)
        return self.state_of.get(stage, "COMPLETED" if ok else "FAILED")

    def _sacct(self, argv):
        self.sacct_calls += 1
        if self.sacct_rc:
            return _done(
                self.sacct_rc, stderr="sacct: error: Problem talking to the database"
            )
        ids = argv[argv.index("--jobs") + 1].split(",")
        lines = []
        for job_id in ids:
            job = self.jobs[job_id]
            state = job["state"] or "PENDING"
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
        if self.on_scancel is not None:
            self.on_scancel(ids)
        if self.scancel_rc and self.scancel_rc.pop(0):
            return _done(1, stderr="scancel: error: Unable to contact slurm controller")
        self.cancelled.extend(ids)
        for job_id in ids:
            if self.jobs[job_id]["state"] in (None, "RUNNING"):
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
    assert [_stage_of(j) for j in jobs] == list(_STAGES)
    assert [j["state"] for j in jobs] == ["COMPLETED"] * len(_STAGES)
    assert jobs[0]["dependency"] is None
    assert "--kill-on-invalid-dep=yes" not in jobs[0]["argv"]
    for before, job in zip(jobs, jobs[1:]):
        assert job["dependency"] == before["id"]
        assert "--kill-on-invalid-dep=yes" in job["argv"]
    for stage, job in zip(_STAGES, jobs):
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
    assert [e["stage"] for e in ended] == list(_STAGES)
    assert [e["slurm_job"] for e in ended] == [j["id"] for j in jobs]
    assert all(e["measured_by"] == "process" for e in ended)
    assert all(e["status"] == "ok" for e in ended)
    assert all(e["threads"] == 1 for e in ended)
    for stage in _STAGES:
        result = pnr_dispatch.read_result(str(run_dir), stage)
        assert result["ok"] is True and result["started"] is True
    # pnr.log is the stage logs in order, as in a local staged run.
    assert (artefacts / "pnr.log").read_bytes() == b"".join(
        (run_dir / f"{_NAMES[stage]}.log").read_bytes() for stage in _STAGES
    )
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["staged"] is True
    assert manifest["dispatch"]["backend"] == "slurm"
    assert list(manifest["dispatch"]["stages"]) == list(_STAGES)
    assert list(manifest["resources"]) == list(_STAGES)
    assert manifest["outcome"]["result"] == "PASS"


def test_dispatch_makes_a_run_staged_whatever_its_config_says(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(
        tmp_path, monkeypatch, cfg=_pnr_cfg(tmp_path, stages=False)
    )

    assert backend.run().results["result"] == "PASS"
    assert len(slurm.submitted()) == len(_STAGES)


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
    for stage in _STAGES:
        script = (run_dir / f"{_NAMES[stage]}.tcl").read_text()
        count = 16 if stage == "detailed_route" else 4
        assert f"set_thread_count {count}\n" in script
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["dispatch"]["stages"]["detailed_route"] == {
        "cpus": 16,
        "mem": "32G",
        "time": "01:00:00",
        "threads_requested": "auto",
        "threads": 16,
    }
    assert manifest["dispatch"]["stages"]["cts"]["threads"] == 4
    ended = {e["stage"]: e for e in _events(run_dir) if e["event"] == "stage_end"}
    assert ended["detailed_route"]["threads"] == 16
    assert ended["cts"]["threads"] == 4
    assert ended["cts"]["thread_plan"] == {
        "requested": "auto",
        "effective": 4,
        "allocation": 4,
        "allocation_source": "SLURM_CPUS_PER_TASK",
    }


def test_threads_auto_takes_the_cpus_the_job_received(tmp_path, monkeypatch):
    # sbatch-args come after the reservation, so the job gets 3 CPUs, not 4.
    slurm = _FakeSlurm(monkeypatch, sbatch_args=["--cpus-per-task=3"])
    cfg = replace(_pnr_cfg(tmp_path, stages=True), threads="auto")
    backend, artefacts, _ = _dispatched(
        tmp_path,
        monkeypatch,
        slurm=slurm,
        cfg=cfg,
        resources=PnrResources(base=DispatchResourcesFile(cpus=4)),
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    run_dir = _only_run(artefacts)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    # The head planned for the CPUs it asked for; each job set what it received.
    assert manifest["dispatch"]["stages"]["cts"]["threads"] == 4
    # Each job ran a copy of its stage script; the recorded one matches the manifest.
    chain = {entry["stage"]: entry["script"] for entry in manifest["chain"]}
    for stage in _STAGES:
        recorded = run_dir / f"{_NAMES[stage]}.tcl"
        assert "set_thread_count 4\n" in recorded.read_text()
        fingerprint = pnr_openroad._file_fingerprint(str(recorded))
        assert fingerprint["sha256"] == chain[stage]["sha256"]
        assert fingerprint["size"] == chain[stage]["size"]
        ran = (run_dir / "dispatch" / f"{_NAMES[stage]}.tcl").read_text()
        assert ran == recorded.read_text().replace(
            "set_thread_count 4\n", "set_thread_count 3\n"
        )
    ended = [e for e in _events(run_dir) if e["event"] == "stage_end"]
    assert all(e["threads"] == 3 for e in ended)
    assert res.results["openroad_threads"] == {
        "requested": "auto",
        "effective": 3,
        "allocation": 3,
        "allocation_source": "SLURM_CPUS_PER_TASK",
    }


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
    for stage in _STAGES:
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
    ("env", "fail_stage"),
    [
        (None, None),
        ({"RB_STUB_FAIL": "global_route"}, None),
        # Exits 0 before its end-of-stage line.
        ({"RB_STUB_EXIT": "check_placement"}, "incomplete"),
        # `finish` exits 0 before `>>> DONE`.
        ({"RB_STUB_EXIT": "filler_placement"}, "incomplete"),
        # Its checkpoint cannot be written.
        ({"RB_STUB_FAIL": "write_db"}, "checkpoint"),
        # An `[ERROR` line in a session that exits 0.
        ({"RB_STUB_ERRLINE": "check_placement"}, None),
    ],
)
def test_the_result_is_the_same_as_the_run_staged_locally(
    tmp_path, monkeypatch, env, fail_stage
):
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
        assert got.get("fail_stage") == expected.get("fail_stage") == fail_stage
        if env.get("RB_STUB_EXIT") == "filler_placement":
            assert got["desc"].startswith("stage 07_finish: the flow did not finish")
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


@pytest.mark.parametrize(
    ("stage", "state"),
    [("finish", "TIMEOUT"), ("cts", "CANCELLED"), ("place", "FAILED")],
)
def test_a_stage_whose_job_did_not_complete_fails_though_it_wrote_its_result(
    tmp_path, monkeypatch, stage, state
):
    # The job wrote a successful result, then Slurm ended it before it exited.
    slurm = _FakeSlurm(monkeypatch, state_of={stage: state})
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)

    res = backend.run()

    run_dir = _only_run(artefacts)
    assert pnr_dispatch.read_result(str(run_dir), stage)["ok"] is True
    jobs = {_stage_of(j): j for j in slurm.submitted()}
    log = pnr_dispatch.job_file(str(run_dir), stage, "slurm.log")
    assert res.results["result"] == "FAIL"
    # The stage whose job did not complete, not the one held behind it.
    assert res.results["desc"] == (
        f"stage {_NAMES[stage]}: Slurm job {jobs[stage]['id']} ended {state} after "
        f"the stage succeeded and wrote its result (see {log})"
    )
    assert res.results["fail_stage"] == "dispatch"
    later = _STAGES[_STAGES.index(stage) + 1 :]
    assert [jobs[s]["state"] for s in later] == ["CANCELLED"] * len(later)
    apply_xfail(res)
    assert res.results["result"] == "FAIL"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["outcome"]["result"] == "FAIL"


def test_a_result_is_taken_as_written_when_accounting_has_no_state(
    tmp_path, monkeypatch, caplog
):
    slurm = _FakeSlurm(monkeypatch, state_of={"finish": "TIMEOUT"})
    slurm.sacct_rc = 1
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)

    with caplog.at_level("WARNING"):
        res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert slurm.sacct_calls == pnr_dispatch._STATE_ATTEMPTS
    (warning,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "pnr.stage_jobs_unaccounted"
    ]
    ids = [j["id"] for j in slurm.submitted()]
    assert warning.rtl_fields["job_ids"] == ids
    assert warning.rtl_fields["stages"] == [_NAMES[s] for s in _STAGES]
    assert warning.getMessage() == (
        f'P&R "demo_pnr": Slurm accounting has no end state for stage job(s) '
        f"{' '.join(ids)}, so the results of "
        f"{', '.join(_NAMES[s] for s in _STAGES)} are taken as the jobs wrote them"
    )


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


def _interrupt_at_first_poll(slurm, signum):
    def _hook():
        slurm.hold = True
        os.kill(os.getpid(), signum)

    slurm.on_poll = _hook


@pytest.mark.parametrize(
    ("signum", "raised"),
    [
        (signal.SIGINT, KeyboardInterrupt),
        (signal.SIGTERM, SystemExit),
        (signal.SIGHUP, SystemExit),
    ],
)
def test_an_interrupt_cancels_this_runs_jobs_and_no_others(
    tmp_path, monkeypatch, signum, raised
):
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    slurm.jobs["9001"] = {"id": "9001", "state": "RUNNING", "dependency": None}
    _interrupt_at_first_poll(slurm, signum)

    with pytest.raises(raised) as caught:
        with pnr_dispatch.cancel_on_interrupt():
            backend.run()

    if raised is SystemExit:
        assert caught.value.code == 128 + signum
    ours = [j["id"] for j in slurm.submitted() if j["id"] != "9001"]
    assert len(ours) == len(_STAGES)
    assert sorted(slurm.cancelled) == sorted(ours)
    assert slurm.jobs["9001"]["state"] == "RUNNING"
    run_dir = _only_run(artefacts)
    events = _events(run_dir)
    assert not [e for e in events if e["event"] == "stage_begin"]
    # The run is finished before rb exits.
    assert events[-1]["event"] == "run_end"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    name = signal.Signals(signum).name
    assert manifest["outcome"]["result"] == "FAIL"
    assert manifest["outcome"]["desc"] == f"dispatch: interrupted by {name}"
    recorded = json.loads((run_dir / "dispatch" / "jobs.json").read_text())
    assert recorded["drained"] is True
    # The handlers are restored.
    assert getattr(signal.getsignal(signum), "__name__", None) != "_handler"


def test_an_interrupt_closes_the_stage_its_job_was_running(tmp_path, monkeypatch):
    slurm = _FakeSlurm(monkeypatch, interrupt_in={"cts": signal.SIGINT})
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)

    with pytest.raises(KeyboardInterrupt):
        with pnr_dispatch.cancel_on_interrupt():
            backend.run()

    jobs = {_stage_of(j): j["id"] for j in slurm.submitted()}
    # The handler cancels every job it has not seen leave the queue, without asking
    # the queue first; the jobs that ended keep their state.
    assert sorted(slurm.cancelled) == sorted(jobs.values())
    states = {stage: slurm.jobs[job_id]["state"] for stage, job_id in jobs.items()}
    assert states == {
        "floorplan": "COMPLETED",
        "place": "COMPLETED",
        **{s: "CANCELLED" for s in ("cts", "global_route", "detailed_route", "finish")},
    }
    run_dir = _only_run(artefacts)
    events = _events(run_dir)
    ended = [e for e in events if e["event"] == "stage_end"]
    assert [e["stage"] for e in ended] == ["floorplan", "place", "cts"]
    cts = ended[-1]
    assert cts["measured_by"] == "scheduler"
    assert cts["slurm_job"] == jobs["cts"]
    assert cts["slurm_state"] == "CANCELLED"
    assert cts["status"] == "error"
    # The job died before it wrote the stage's log; the head cut it from pnr.log.
    log = (run_dir / "03_cts.log").read_bytes()
    begun = [e for e in events if e["event"] == "stage_begin"][-1]
    pnr_log = (artefacts / "pnr.log").read_bytes()
    assert log == pnr_log[begun["log_offset"] :]
    assert b"before the job ended" in log
    assert events[-1]["event"] == "run_end"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["outcome"]["desc"] == (
        "dispatch: interrupted by SIGINT; stage 03_cts's Slurm job "
        f"{jobs['cts']} was cancelled during the stage"
    )


def _record_listings(monkeypatch) -> list:
    """Record each directory listed, with the id of the job listing it (None for the head)."""
    seen = []
    listdir = os.listdir

    def _listdir(path="."):
        seen.append((os.environ.get("SLURM_JOB_ID"), os.path.abspath(path)))
        return listdir(path)

    monkeypatch.setattr(os, "listdir", _listdir)
    return seen


@pytest.mark.parametrize("interrupt", [None, signal.SIGINT])
def test_the_head_and_each_job_open_the_directories_jobs_write(
    tmp_path, monkeypatch, interrupt
):
    # On NFS, a host sees a file another host created under a name it looked up
    # while the file was missing only once it opens the directory.
    slurm = _FakeSlurm(monkeypatch, interrupt_in=interrupt and {"cts": interrupt})
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)
    seen = _record_listings(monkeypatch)

    if interrupt is None:
        assert backend.run().results["result"] == "PASS"
    else:
        with pytest.raises(KeyboardInterrupt):
            with pnr_dispatch.cancel_on_interrupt():
                backend.run()

    run_dir = str(_only_run(artefacts))
    written = {run_dir, os.path.join(run_dir, "dispatch"), str(artefacts)}
    ran = [j["id"] for j in slurm.submitted() if j["state"] != "CANCELLED"]
    if interrupt is not None:
        ran.append(next(j["id"] for j in slurm.submitted() if _stage_of(j) == "cts"))
    for job_id in ran:
        assert written <= {path for job, path in seen if job == job_id}
    last_job = max(i for i, (job, _) in enumerate(seen) if job is not None)
    assert written <= {path for job, path in seen[last_job:] if job is None}


def test_a_repeated_signal_waits_for_the_cancel(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    _interrupt_at_first_poll(slurm, signal.SIGINT)
    calls = []

    def _again(ids):
        calls.append(ids)
        # Ctrl-C again while scancel is still talking to the controller.
        os.kill(os.getpid(), signal.SIGINT)

    slurm.on_scancel = _again

    with pytest.raises(KeyboardInterrupt):
        with pnr_dispatch.cancel_on_interrupt():
            backend.run()

    ours = [j["id"] for j in slurm.submitted()]
    assert len(calls) == 1
    assert sorted(slurm.cancelled) == sorted(ours)
    run_dir = _only_run(artefacts)
    assert _events(run_dir)[-1]["event"] == "run_end"


def test_a_refused_cancel_is_tried_again(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    _interrupt_at_first_poll(slurm, signal.SIGINT)
    slurm.scancel_rc = [1]

    with pytest.raises(KeyboardInterrupt):
        with pnr_dispatch.cancel_on_interrupt():
            backend.run()

    assert sorted(slurm.cancelled) == sorted(j["id"] for j in slurm.submitted())


def test_a_chain_counts_as_cancelled_only_once_the_scheduler_took_it():
    calls = []

    class _Backend:
        name = "slurm"

        def queue_states(self, handles, **_):
            return None

        def cancel_all(self, handles, **_):
            calls.append([h.job_id for h in handles])
            return len(calls) > 1

    chain = pnr_dispatch.StageChain(_Backend())
    chain.handles.append(JobHandle(job_id="7", spec=SimpleNamespace(stage="cts")))

    assert chain.cancel() is False
    assert chain.cancel() is True
    assert chain.cancel() is True
    assert calls == [["7"], ["7"]]


def test_an_interrupt_during_sbatch_cancels_the_job_sbatch_accepts_later(
    tmp_path, monkeypatch
):
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    slurm.sbatch_waits = 3
    slurm.on_sbatch_wait = lambda guard: os.kill(os.getpid(), signal.SIGINT)

    with pytest.raises(KeyboardInterrupt):
        with pnr_dispatch.cancel_on_interrupt():
            backend.run()

    # rb returned without waiting for sbatch; the run is finished.
    (guard,) = slurm.guards
    assert slurm.submitted() == []
    run_dir = _only_run(artefacts)
    assert _events(run_dir)[-1]["event"] == "run_end"
    recorded = json.loads((run_dir / "dispatch" / "jobs.json").read_text())
    assert recorded["drained"] is False
    # sbatch answers after rb gave it up: the job it accepted is cancelled.
    guard.finish()
    (job,) = slurm.submitted()
    assert slurm.cancelled == [job["id"]]
    assert (run_dir / "dispatch" / "01_floorplan.sbatch").read_text().strip() == (
        job["id"]
    )


def test_an_interrupt_stops_dispatching_in_worker_threads(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    slurm.sbatch_waits = 10**9
    in_sbatch = threading.Event()

    def _waiting(guard):
        in_sbatch.set()
        threading.Event().wait(0.01)

    slurm.on_sbatch_wait = _waiting
    rows = []
    worker = threading.Thread(target=lambda: rows.append(backend.run()))

    with pytest.raises(KeyboardInterrupt):
        with pnr_dispatch.cancel_on_interrupt():
            worker.start()
            assert in_sbatch.wait(10)
            os.kill(os.getpid(), signal.SIGINT)
            # A few bytecodes so the handler runs here.
            for _ in range(1000):
                pass

    # The worker gives its submission up at its next look, and finishes its run.
    worker.join(10)
    assert not worker.is_alive()
    (res,) = rows
    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "dispatch"
    assert res.results["desc"] == "dispatch: interrupted by SIGINT"
    (guard,) = slurm.guards
    guard.waits = 0
    guard.finish()
    assert slurm.cancelled == [j["id"] for j in slurm.submitted()]
    # A run that reaches its chain after the interrupt submits nothing.
    with pytest.raises(pnr_dispatch.ChainCancelled, match="interrupted by SIGINT"):
        with pnr_dispatch.StageChain(slurm.backend):
            pass


def test_a_failed_wait_cancels_the_chain_and_fails_the_run(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)

    def _give_up():
        slurm.hold = True
        raise FatalRtlBuddyError("the queue could not be read")

    slurm.on_poll = _give_up

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "dispatch"
    assert res.results["desc"] == "dispatch: the queue could not be read"
    assert sorted(slurm.cancelled) == sorted(j["id"] for j in slurm.submitted())


class _Clock:
    def __init__(self, step):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def test_max_wait_bounds_a_stage_jobs_wait_in_the_queue(tmp_path, monkeypatch):
    slurm = _FakeSlurm(monkeypatch, max_wait=600.0)
    slurm.hold = True
    monkeypatch.setattr(pnr_dispatch, "_clock", _Clock(100))
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "dispatch"
    first = slurm.submitted()[0]["id"]
    assert re.fullmatch(
        rf"dispatch: stage 01_floorplan: Slurm job {first} waited \d+s in the "
        r"queue without starting, longer than cfg-dispatch.max-wait \(600s\); "
        rf"`squeue -j {first}` gives the reason it is held",
        res.results["desc"],
    )
    assert sorted(slurm.cancelled) == sorted(j["id"] for j in slurm.submitted())


def _queue_down(slurm, *, answers_after=None):
    """Make squeue fail until `slurm.hold` is set, or until it was asked `answers_after` times; record the polls in `slurm.down_polls`."""
    real = slurm._squeue
    slurm.down_polls = 0

    def _squeue(argv):
        if slurm.hold or (
            answers_after is not None and slurm.down_polls >= answers_after
        ):
            return real(argv)
        slurm.down_polls += 1
        return _done(1, stderr="squeue: error: Unable to contact slurm controller")

    slurm._squeue = _squeue
    real_cancel = slurm._scancel

    def _scancel(argv):
        # The controller answers again once rb cancels.
        slurm.hold = True
        return real_cancel(argv)

    slurm._scancel = _scancel


def test_max_wait_runs_while_the_queue_cannot_be_read(tmp_path, monkeypatch):
    # Accounting says the job is still pending.
    slurm = _FakeSlurm(monkeypatch, max_wait=600.0)
    monkeypatch.setattr(pnr_dispatch, "_clock", _Clock(100))
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)
    _queue_down(slurm)

    res = backend.run()

    assert res.results["fail_stage"] == "dispatch"
    first = slurm.submitted()[0]["id"]
    assert re.fullmatch(
        rf"dispatch: stage 01_floorplan: Slurm job {first} waited \d+s in the "
        r"queue without starting, longer than cfg-dispatch.max-wait \(600s\); "
        rf"`squeue -j {first}` gives the reason it is held",
        res.results["desc"],
    )
    assert slurm.sacct_calls >= 1
    assert sorted(slurm.cancelled) == sorted(j["id"] for j in slurm.submitted())


def test_max_wait_keeps_a_job_accounting_saw_start(tmp_path, monkeypatch):
    # Seen pending once, the first job starts while squeue cannot be read for far
    # longer than max-wait; accounting says it runs.
    slurm = _FakeSlurm(monkeypatch, max_wait=600.0)
    monkeypatch.setattr(pnr_dispatch, "_clock", _Clock(10))
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)
    real = slurm._squeue
    asked = []

    def _squeue(argv):
        asked.append(argv)
        first = slurm.jobs[min(slurm.jobs)]
        if len(asked) == 1:
            slurm.hold = True
            return real(argv)
        if len(asked) < 300:
            first["state"] = "RUNNING"
            return _done(1, stderr="squeue: error: Unable to contact slurm controller")
        # The controller answers again and the chain runs.
        first["state"], slurm.hold = None, False
        return real(argv)

    slurm._squeue = _squeue

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert slurm.sacct_calls >= 1
    assert slurm.cancelled == []


def test_max_wait_without_queue_or_accounting_waits_out_the_jobs_time(
    tmp_path, monkeypatch
):
    slurm = _FakeSlurm(monkeypatch, max_wait=600.0)
    slurm.sacct_rc = 1
    monkeypatch.setattr(pnr_dispatch, "_clock", _Clock(10))
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)
    _queue_down(slurm)

    res = backend.run()

    assert res.results["fail_stage"] == "dispatch"
    first = slurm.submitted()[0]["id"]
    found = re.fullmatch(
        rf"dispatch: stage 01_floorplan: Slurm job {first} could not be seen to "
        r"start; the queue has not answered for (\d+)s, accounting has no state "
        r"for the job, and it has been queued longer than cfg-dispatch.max-wait "
        r"\(600s\) plus its time \(00:30:00\)",
        res.results["desc"],
    )
    assert found, res.results["desc"]
    # Not given up at max-wait (600s), only once the job's 30 minutes could have
    # run too.
    assert int(found.group(1)) > 2000
    assert sorted(slurm.cancelled) == sorted(j["id"] for j in slurm.submitted())


def test_max_wait_does_not_bound_a_running_stage_job(tmp_path, monkeypatch):
    slurm = _FakeSlurm(monkeypatch, max_wait=600.0)
    monkeypatch.setattr(pnr_dispatch, "_clock", _Clock(100))
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)
    real = slurm._squeue

    def _squeue(argv):
        first = slurm.jobs[min(slurm.jobs)]
        # The first job runs for 20 polls, far past max-wait.
        slurm.hold = slurm.polls < 20
        first["state"] = "RUNNING" if slurm.hold else None
        return real(argv)

    slurm._squeue = _squeue

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert slurm.cancelled == []


def test_a_queue_poll_that_hangs_counts_as_the_queue_not_answering(
    tmp_path, monkeypatch
):
    slurm = _FakeSlurm(monkeypatch, max_wait=600.0)
    monkeypatch.setattr(pnr_dispatch, "_clock", _Clock(100))
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)
    real = slurm._squeue
    bounds = []

    def _hung(argv):
        if slurm.hold:
            return real(argv)
        (_, timeout) = slurm.calls[-1]
        bounds.append(timeout)
        if timeout is None:
            pytest.fail("a poll of the wait has no timeout, so a hung squeue holds it")
        raise subprocess.TimeoutExpired(argv, timeout)

    slurm._squeue = _hung
    real_cancel = slurm._scancel

    def _scancel(argv):
        slurm.hold = True
        return real_cancel(argv)

    slurm._scancel = _scancel

    res = backend.run()

    # Every poll gave up at its timeout, so max-wait still ran out.
    assert res.results["fail_stage"] == "dispatch"
    first = slurm.submitted()[0]["id"]
    assert re.fullmatch(
        rf"dispatch: stage 01_floorplan: Slurm job {first} waited \d+s in the "
        r"queue without starting, longer than cfg-dispatch.max-wait \(600s\); "
        rf"`squeue -j {first}` gives the reason it is held",
        res.results["desc"],
    )
    # The wait's polls, then the cancel's.
    assert len(bounds) > 1
    assert bounds == [pnr_dispatch._WAIT_POLL_TIMEOUT_S] * (len(bounds) - 1) + [
        CANCEL_TIMEOUT_S
    ]
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

        def submit_pnr_stage(self, spec, *, dependency=None, abandon=None):
            handle = JobHandle(job_id=str(len(chain.handles) + 1), spec=spec)
            if spec.stage == "place":
                chain.cancel()
            return handle

        def queue_states(self, handles, **_):
            return None

        def cancel_all(self, handles, **_):
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
    assert [_stage_of(j) for j in slurm.submitted()] == list(_STAGES[:4])


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


def test_a_stage_job_runs_only_for_an_unfinished_run_of_its_version(
    tmp_path, monkeypatch
):
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

    manifest_path = Path(run_dir, "manifest.json")
    manifest = json.loads(manifest_path.read_text())
    manifest["outcome"] = None
    manifest_path.write_text(json.dumps(manifest))
    assert pnr_dispatch._refusal(run_dir, "finish") is None
    manifest["generator"] = "rtl-buddy 0.0.1"
    manifest_path.write_text(json.dumps(manifest))
    assert pnr_dispatch._refusal(run_dir, "finish") == (
        f"the job runs {pnr_dispatch.generator()}, but the run was started by "
        "rtl-buddy 0.0.1; run the head and its jobs from the same rtl_buddy "
        "installation"
    )


def test_a_job_of_another_rtl_buddy_version_refuses_its_stage(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    monkeypatch.setattr(pnr_dispatch, "generator", lambda: "rtl-buddy 0.0.1")

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "dispatch"
    assert res.results["desc"].startswith(
        "stage 01_floorplan: the job runs rtl-buddy 0.0.1, but the run "
        "was started by rtl-buddy "
    )
    run_dir = _only_run(artefacts)
    assert not [e for e in _events(run_dir) if e["event"] == "stage_begin"]


def _leave_running(artefacts, slurm, *, job_id):
    """Make the dispatched run in `artefacts` look as if its head was killed while job `job_id` ran."""
    run_dir = _only_run(artefacts)
    path = run_dir / "dispatch" / "jobs.json"
    recorded = json.loads(path.read_text())
    recorded["drained"] = False
    path.write_text(json.dumps(recorded))
    slurm.jobs[job_id]["state"] = "RUNNING"
    slurm.hold = True
    return run_dir


@pytest.mark.parametrize("dispatched", [True, False])
def test_a_run_waits_for_the_jobs_of_a_killed_head(tmp_path, monkeypatch, dispatched):
    first, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    assert first.run().results["result"] == "PASS"
    last = slurm.submitted()[-1]["id"]
    run_dir = _leave_running(artefacts, slurm, job_id=last)
    log = (artefacts / "pnr.log").read_bytes()
    if dispatched:
        again, _, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)
    else:
        again, _ = _backend(tmp_path, monkeypatch, stages=True)
        again.root_cfg.get_dispatch_cfg.return_value = DispatchConfigFile().initialise()

    res = again.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "dispatch"
    assert res.results["desc"] == (
        f"dispatch: stage job(s) {last} of earlier dispatched run(s) "
        f"{run_dir.name} are still queued or running and would write into this "
        f"run's outputs; wait for them to end or cancel them with `scancel {last}`"
    )
    # Nothing was cleared or started.
    assert _runs(artefacts) == [run_dir]
    assert (artefacts / "pnr.log").read_bytes() == log
    assert slurm.cancelled == []

    slurm.jobs[last]["state"] = "COMPLETED"
    slurm.hold = False
    assert again.run().results["result"] == "PASS"
    recorded = json.loads((run_dir / "dispatch" / "jobs.json").read_text())
    assert recorded["drained"] is True


# `.sbatch.tmp`: the submission guard was stopped after sbatch answered, before it
# filed the answer.
@pytest.mark.parametrize("answer", ["sbatch", "sbatch.tmp"])
def test_a_job_accepted_after_its_head_died_is_found(tmp_path, monkeypatch, answer):
    first, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    assert first.run().results["result"] == "PASS"
    run_dir = _leave_running(artefacts, slurm, job_id=slurm.submitted()[0]["id"])
    slurm.jobs[slurm.submitted()[0]["id"]]["state"] = "COMPLETED"
    # sbatch answered after the head was killed; jobs.json never heard of the job.
    slurm.jobs["777"] = {"id": "777", "state": "RUNNING", "dependency": None}
    (run_dir / "dispatch" / f"05_detailed_route.{answer}").write_text("777\n")
    again, _, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)
    # A run that wrongly starts fails at max-wait instead of waiting for ever.
    slurm.backend.max_wait = 600.0
    monkeypatch.setattr(pnr_dispatch, "_clock", _Clock(100))

    res = again.run()

    assert res.results["fail_stage"] == "dispatch"
    assert "stage job(s) 777 of earlier" in res.results["desc"]


def _leave_unanswered(artefacts, *suffixes):
    """Make the dispatched run in `artefacts` look as if its head was killed before sbatch answered its first submission: no job recorded, and an empty `01_floorplan.<suffix>` for each of `suffixes`."""
    run_dir = _only_run(artefacts)
    dispatch = run_dir / "dispatch"
    (dispatch / "jobs.json").unlink()
    for answer in dispatch.glob("*.sbatch"):
        answer.unlink()
    for suffix in suffixes:
        (dispatch / f"01_floorplan.{suffix}").write_text("")
    return run_dir


def _without_squeue(monkeypatch):
    which = pnr_openroad.shutil.which
    monkeypatch.setattr(
        pnr_openroad.shutil,
        "which",
        lambda cmd, *a, **k: None if cmd == "squeue" else which(cmd, *a, **k),
    )


# `.sbatch.tmp`: the guard still waits on sbatch; `.sbatch.cancel`: the head gave the
# submission up, and the guard cancels the job once sbatch answers.
@pytest.mark.parametrize(
    ("suffixes", "dispatched"),
    [
        (("sbatch.tmp",), True),
        (("sbatch.cancel",), True),
        (("sbatch.tmp", "sbatch.cancel"), True),
        (("sbatch.tmp",), False),
    ],
)
def test_a_submission_sbatch_has_not_answered_blocks_a_later_run(
    tmp_path, monkeypatch, suffixes, dispatched
):
    first, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    assert first.run().results["result"] == "PASS"
    run_dir = _leave_unanswered(artefacts, *suffixes)
    log = (artefacts / "pnr.log").read_bytes()
    slurm.calls.clear()
    if dispatched:
        again, _, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)
    else:
        # No job id to look up, so no scheduler client is needed to refuse.
        again, _ = _backend(tmp_path, monkeypatch, stages=True)
        _without_squeue(monkeypatch)

    res = again.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "dispatch"
    named = run_dir / "dispatch" / f"01_floorplan.{suffixes[0]}"
    assert res.results["desc"] == (
        f"dispatch: stage job submission(s) {named} of earlier dispatched run(s) "
        f"{run_dir.name} have no answer from sbatch yet, and a job sbatch accepts "
        "would write into this run's outputs; run rb pnr again once sbatch has "
        "answered (a submission without an answer for 660s is taken to have "
        "failed), or once the submission is known to have died remove "
        f"{run_dir / 'dispatch'} to run without it"
    )
    # Nothing was cleared, started or asked of Slurm.
    assert _runs(artefacts) == [run_dir]
    assert (artefacts / "pnr.log").read_bytes() == log
    assert slurm.calls == []
    if not dispatched:
        return

    # sbatch answers with a job that has already ended.
    (run_dir / "dispatch" / "01_floorplan.sbatch.tmp").unlink(missing_ok=True)
    (run_dir / "dispatch" / "01_floorplan.sbatch").write_text(
        f"{slurm.submitted()[0]['id']}\n"
    )
    assert again.run().results["result"] == "PASS"
    recorded = json.loads((run_dir / "dispatch" / "jobs.json").read_text())
    assert recorded["drained"] is True


def test_a_submission_without_an_answer_for_long_is_taken_to_have_failed(
    tmp_path, monkeypatch, caplog
):
    first, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    assert first.run().results["result"] == "PASS"
    run_dir = _leave_unanswered(artefacts, "sbatch.tmp")
    unanswered = run_dir / "dispatch" / "01_floorplan.sbatch.tmp"
    then = time.time() - pnr_dispatch._UNRESOLVED_AFTER_S - 5
    os.utime(unanswered, (then, then))
    again, _, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)

    with caplog.at_level("WARNING"):
        assert again.run().results["result"] == "PASS"

    (warning,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "pnr.stage_submission_presumed_failed"
    ]
    assert warning.rtl_fields["file"] == str(unanswered)
    assert re.fullmatch(
        rf"P&R: stage job submission {re.escape(str(unanswered))} of the earlier "
        rf"dispatched run {re.escape(str(run_dir))} has had no answer from sbatch "
        r"for 11m0\ds, more than the 11m00s a submission is given, so it is taken "
        r"to have failed",
        warning.getMessage(),
    ), warning.getMessage()
    # Recorded as drained, so the next run does not warn again.
    recorded = json.loads((run_dir / "dispatch" / "jobs.json").read_text())
    assert recorded["drained"] is True


def test_the_orphan_lookup_asks_only_for_this_users_jobs(tmp_path, monkeypatch):
    first, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    assert first.run().results["result"] == "PASS"
    _leave_running(artefacts, slurm, job_id=slurm.submitted()[-1]["id"])
    slurm.calls.clear()
    again, _, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)

    assert again.run().results["fail_stage"] == "dispatch"

    (argv, timeout), *_ = [c for c in slurm.calls if c[0][0] == "squeue"]
    assert f"--user={os.getuid()}" in argv
    assert timeout == pnr_dispatch._ORPHAN_POLL_TIMEOUT_S


def test_a_queue_that_cannot_be_read_refuses_the_run_and_says_so(tmp_path, monkeypatch):
    first, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    assert first.run().results["result"] == "PASS"
    ids = " ".join(j["id"] for j in slurm.submitted())
    run_dir = _leave_running(artefacts, slurm, job_id=slurm.submitted()[-1]["id"])
    log = (artefacts / "pnr.log").read_bytes()
    slurm._squeue = lambda argv: _done(
        1, stderr="squeue: error: Unable to contact slurm controller"
    )
    again, _, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)
    # A run that wrongly starts fails at max-wait instead of waiting for ever.
    slurm.backend.max_wait = 600.0
    monkeypatch.setattr(pnr_dispatch, "_clock", _Clock(100))

    res = again.run()

    assert res.results["fail_stage"] == "dispatch"
    assert res.results["desc"] == (
        f"dispatch: cannot tell whether stage job(s) {ids} of earlier dispatched "
        f"run(s) {run_dir.name} are still queued: squeue: error: Unable to contact "
        "slurm controller; run rb pnr again once squeue answers or cancel them "
        f"with `scancel {ids}`; once they have ended, removing "
        f"{run_dir / 'dispatch'} lets rb pnr run without them"
    )
    assert _runs(artefacts) == [run_dir]
    assert (artefacts / "pnr.log").read_bytes() == log
    recorded = json.loads((run_dir / "dispatch" / "jobs.json").read_text())
    assert recorded["drained"] is False


@pytest.mark.parametrize("age_h", [0, 4])
def test_a_host_without_slurm_waits_out_the_time_the_jobs_asked_for(
    tmp_path, monkeypatch, caplog, age_h
):
    # Six jobs of 30 minutes each: the chain could run for 3 hours after its record
    # last changed.
    first, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    assert first.run().results["result"] == "PASS"
    ids = " ".join(j["id"] for j in slurm.submitted())
    run_dir = _leave_running(artefacts, slurm, job_id=slurm.submitted()[-1]["id"])
    then = time.time() - age_h * 3600
    for path in (run_dir / "dispatch").iterdir():
        os.utime(path, (then, then))
    again, _ = _backend(tmp_path, monkeypatch, stages=True)
    which = pnr_openroad.shutil.which
    monkeypatch.setattr(
        pnr_openroad.shutil,
        "which",
        lambda cmd, *a, **k: None if cmd == "squeue" else which(cmd, *a, **k),
    )

    with caplog.at_level("WARNING"):
        res = again.run()

    unchecked = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "pnr.stage_jobs_unchecked"
    ]
    if age_h == 0:
        assert res.results["fail_stage"] == "dispatch"
        assert res.results["desc"] == (
            f"dispatch: stage job(s) {ids} of earlier dispatched run(s) "
            f"{run_dir.name} may still be queued or running, and this host has no "
            "Slurm client to look them up (squeue not found); run rb pnr where "
            "squeue answers, or once they have ended remove "
            f"{run_dir / 'dispatch'} to run without them"
        )
        assert _runs(artefacts) == [run_dir]
        assert not unchecked
    else:
        assert res.results["result"] == "PASS", res.results
        (warning,) = unchecked
        assert warning.rtl_fields["chain_s"] == 6 * 1800
        assert warning.rtl_fields["clear"] == str(run_dir / "dispatch")
    recorded = json.loads((run_dir / "dispatch" / "jobs.json").read_text())
    assert recorded["drained"] is False


def test_cancelling_bounds_each_scheduler_command(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    _interrupt_at_first_poll(slurm, signal.SIGINT)

    with pytest.raises(KeyboardInterrupt):
        with pnr_dispatch.cancel_on_interrupt():
            backend.run()

    commands = [(argv[0], timeout) for argv, timeout in slurm.calls]
    first_cancel = commands.index(("scancel", CANCEL_TIMEOUT_S))
    # The handler cancels without asking the queue first: the command before is the
    # wait's own poll. Every later command is bounded too.
    assert commands[first_cancel - 1] == (
        "squeue",
        pnr_dispatch._WAIT_POLL_TIMEOUT_S,
    )
    assert [t for c, t in commands[first_cancel:] if c in ("squeue", "scancel")]
    assert all(
        t is not None for c, t in commands[first_cancel:] if c in ("squeue", "scancel")
    )


def test_a_scancel_that_does_not_answer_counts_as_refused(monkeypatch, caplog):
    slurm = _FakeSlurm(monkeypatch)

    def _wedged(argv):
        raise subprocess.TimeoutExpired(argv, CANCEL_TIMEOUT_S)

    slurm._scancel = _wedged
    handle = JobHandle(job_id="7", spec=SimpleNamespace(suite_dir=None))

    with caplog.at_level("WARNING"):
        assert slurm.backend.cancel_all([handle], timeout_s=CANCEL_TIMEOUT_S) is False

    events = [getattr(r, "rtl_event", None) for r in caplog.records]
    assert "dispatch.cancel_failed" in events
    assert "dispatch.cancelled" not in events


def test_an_exception_after_sbatch_answered_cancels_its_job(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    real = slurm_module.log_event

    def _log_event(logger, level, event, **fields):
        if event == "dispatch.pnr_stage_submitted" and fields.get("stage") == "place":
            # The interrupt lands after sbatch answered, before the chain has the job.
            slurm.hold = True
            raise KeyboardInterrupt
        return real(logger, level, event, **fields)

    monkeypatch.setattr(slurm_module, "log_event", _log_event)

    with pytest.raises(KeyboardInterrupt):
        backend.run()

    place = next(j["id"] for j in slurm.submitted() if _stage_of(j) == "place")
    assert sorted(slurm.cancelled) == sorted(j["id"] for j in slurm.submitted())
    recorded = json.loads((_only_run(artefacts) / "dispatch" / "jobs.json").read_text())
    assert place in [j["job_id"] for j in recorded["jobs"]]
    assert recorded["drained"] is True


def test_the_cancel_after_max_wait_blocks_signals_for_scancel(tmp_path, monkeypatch):
    slurm = _FakeSlurm(monkeypatch, max_wait=600.0)
    slurm.hold = True
    monkeypatch.setattr(pnr_dispatch, "_clock", _Clock(100))
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)
    masks = []
    slurm.on_scancel = lambda ids: masks.append(
        signal.pthread_sigmask(signal.SIG_BLOCK, [])
    )

    res = backend.run()

    assert res.results["fail_stage"] == "dispatch"
    # scancel inherits the mask, so the terminal's Ctrl-C cannot kill it halfway.
    assert masks
    assert all({signal.SIGINT, signal.SIGTERM, signal.SIGHUP} <= m for m in masks)


def test_an_interrupt_during_the_max_wait_cancel_keeps_its_reason(
    tmp_path, monkeypatch
):
    slurm = _FakeSlurm(monkeypatch, max_wait=600.0)
    slurm.hold = True
    monkeypatch.setattr(pnr_dispatch, "_clock", _Clock(100))
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)
    calls = []

    def _interrupt(ids):
        calls.append(ids)
        os.kill(os.getpid(), signal.SIGINT)

    slurm.on_scancel = _interrupt

    with pytest.raises(KeyboardInterrupt):
        with pnr_dispatch.cancel_on_interrupt():
            backend.run()

    assert len(calls) == 1
    manifest = json.loads((_only_run(artefacts) / "manifest.json").read_text())
    assert "longer than cfg-dispatch.max-wait (600s)" in manifest["outcome"]["desc"]


def test_a_second_signal_ends_the_wait_for_the_cancelled_jobs(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    monkeypatch.setattr(pnr_dispatch, "_clock", _Clock(1))
    _interrupt_at_first_poll(slurm, signal.SIGINT)
    real = slurm._squeue

    def _squeue(argv):
        out = real(argv)
        if slurm.polls == 3:
            # Ctrl-C again while rb waits for the cancelled jobs to leave the queue.
            os.kill(os.getpid(), signal.SIGINT)
        return out

    slurm._squeue = _squeue

    def _taken_but_queued(argv):
        # Slurm takes the cancel, but the jobs stay listed.
        slurm.cancelled.extend(a for a in argv[1:] if not a.startswith("-"))
        return _done()

    slurm._scancel = _taken_but_queued

    with pytest.raises(KeyboardInterrupt):
        with pnr_dispatch.cancel_on_interrupt():
            backend.run()

    # Without the second signal the wait would poll until its 30 s ran out.
    assert slurm.polls == 3
    run_dir = _only_run(artefacts)
    assert _events(run_dir)[-1]["event"] == "run_end"
    recorded = json.loads((run_dir / "dispatch" / "jobs.json").read_text())
    assert recorded["drained"] is False


def test_a_repeated_signal_ends_a_wait_in_a_worker_thread():
    class _Backend:
        name = "slurm"

        def queue_states(self, handles, **_):
            return {pnr_dispatch.telemetry_key(h): "running" for h in handles}

    chain = pnr_dispatch.StageChain(_Backend())
    chain.handles.append(JobHandle(job_id="7", spec=SimpleNamespace(stage="cts")))
    left = []
    # As under `rb pnr -j`: the main thread takes the signals, a worker waits.
    worker = threading.Thread(target=lambda: left.append(chain.drain(60)), daemon=True)

    with pnr_dispatch.cancel_on_interrupt():
        for repeat in (False, True):
            with pytest.raises(KeyboardInterrupt):
                os.kill(os.getpid(), signal.SIGINT)
                for _ in range(1000):
                    pass
            if not repeat:
                worker.start()
                worker.join(0.5)
                assert worker.is_alive()
        worker.join(10)

    assert not worker.is_alive()
    assert [h.job_id for h in left[0]] == ["7"]


def test_a_job_added_after_a_cancel_is_cancelled_and_retried():
    calls = []

    class _Backend:
        name = "slurm"

        def queue_states(self, handles, **_):
            return None

        def cancel_all(self, handles, **_):
            calls.append([h.job_id for h in handles])
            # Job 8's first cancel is refused.
            return calls.count(["8"]) != 1 or calls[-1] != ["8"]

    chain = pnr_dispatch.StageChain(_Backend())
    spec = SimpleNamespace(stage="cts")
    chain.handles.append(JobHandle(job_id="7", spec=spec))
    assert chain.cancel() is True
    # A submission in a worker thread lands after the cancel.
    chain.handles.append(JobHandle(job_id="8", spec=spec))

    assert chain.cancel() is False
    assert chain.cancel() is True
    assert chain.cancel() is True
    assert calls == [["7"], ["8"], ["8"]]


def test_an_unexpected_error_settles_the_chain_and_is_raised(tmp_path, monkeypatch):
    backend, artefacts, slurm = _dispatched(tmp_path, monkeypatch)
    real = pnr_dispatch.record_jobs
    calls = []

    def _record(run_dir, chain, *, drained=False):
        calls.append(drained)
        if len(calls) == 2:
            slurm.hold = True
            raise OSError(28, "No space left on device")
        real(run_dir, chain, drained=drained)

    monkeypatch.setattr(pnr_dispatch, "record_jobs", _record)

    with pytest.raises(OSError):
        backend.run()

    assert sorted(slurm.cancelled) == sorted(j["id"] for j in slurm.submitted())
    run_dir = _only_run(artefacts)
    assert _events(run_dir)[-1]["event"] == "run_end"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["outcome"]["desc"] == (
        "dispatch: OSError: [Errno 28] No space left on device"
    )
    recorded = json.loads((run_dir / "dispatch" / "jobs.json").read_text())
    assert recorded["drained"] is True


def test_a_signal_while_the_chains_end_is_read_finishes_the_run(tmp_path, monkeypatch):
    slurm = _FakeSlurm(monkeypatch, died={"cts": "OUT_OF_MEMORY"})
    backend, artefacts, _ = _dispatched(tmp_path, monkeypatch, slurm=slurm)
    real = slurm._sacct

    def _sacct(argv):
        if slurm.sacct_calls == 0:
            os.kill(os.getpid(), signal.SIGINT)
        return real(argv)

    slurm._sacct = _sacct

    with pytest.raises(KeyboardInterrupt):
        with pnr_dispatch.cancel_on_interrupt():
            backend.run()

    run_dir = _only_run(artefacts)
    events = _events(run_dir)
    assert events[-1]["event"] == "run_end"
    # The killed stage was closed as the chain's end was read, before the signal.
    cts = [e for e in events if e["event"] == "stage_end" and e["stage"] == "cts"]
    assert [e["slurm_state"] for e in cts] == ["OUT_OF_MEMORY"]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["outcome"]["desc"] == "dispatch: interrupted by SIGINT"
    recorded = json.loads((run_dir / "dispatch" / "jobs.json").read_text())
    assert recorded["drained"] is True


@pytest.mark.parametrize("cancelled", [True, False])
def test_the_cancel_messages_say_whether_slurm_took_the_cancel(cancelled):
    survived = logging_utils._human_message(
        "pnr.stage_jobs_survived_cancel",
        {
            "pnr": "blk",
            "job_ids": ["7"],
            "cancelled": cancelled,
            "command": "scancel 7",
        },
    )
    abandoned = logging_utils._human_message(
        "dispatch.pnr_stage_submission_abandoned",
        {"pnr": "blk", "stage": "cts", "job_id": "7", "cancelled": cancelled},
    )
    if cancelled:
        assert "after rb cancelled them" in survived
        assert abandoned.endswith("; cancelled job 7, which it had accepted")
    else:
        assert "Slurm did not take rb's cancel" in survived
        assert abandoned.endswith(
            "; it had accepted job 7, and scancel did not take the cancel: run "
            "`scancel 7`"
        )


def test_a_run_without_resources_warns_what_its_jobs_get(tmp_path, monkeypatch, caplog):
    backend, _, _ = _dispatched(tmp_path, monkeypatch)
    with caplog.at_level("INFO"):
        assert backend.run().results["result"] == "PASS"

    (warning,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "pnr.dispatch_resources_default"
    ]
    assert warning.rtl_fields == {
        "pnr": "demo_pnr",
        "cpus": 2,
        "mem": "4G",
        "time": "00:30:00",
    }
    assert "has no resources: block" in warning.getMessage()


def test_the_console_names_each_job_and_the_run(tmp_path, monkeypatch, caplog):
    backend, _, slurm = _dispatched(
        tmp_path,
        monkeypatch,
        resources=PnrResources(base=DispatchResourcesFile(cpus=1)),
    )
    # The console's first line would set logging up and drop caplog's handler.
    logging_utils.setup_logging(color=False)
    logging.getLogger().addHandler(caplog.handler)
    with caplog.at_level("INFO"):
        assert backend.run().results["result"] == "PASS"

    events = [r for r in caplog.records if getattr(r, "rtl_event", None)]
    assert not [r for r in events if r.rtl_event == "pnr.dispatch_resources_default"]
    submitted = [r for r in events if r.rtl_event == "pnr.stage_job_submitted"]
    assert [r.rtl_fields["job_id"] for r in submitted] == [
        j["id"] for j in slurm.submitted()
    ]
    assert submitted[1].getMessage() == (
        f'P&R "demo_pnr": stage 02_place is Slurm job {submitted[1].rtl_fields["job_id"]}, '
        f"held until job {submitted[0].rtl_fields['job_id']} succeeds"
    )
    (drained,) = [r for r in events if r.rtl_event == "pnr.stage_jobs_drained"]
    assert drained.getMessage().startswith(
        'P&R "demo_pnr": all 6 stage jobs have left the queue'
    )
    assert logging_utils._human_message(
        "pnr.stage_jobs_drained", {"pnr": "demo_pnr", "jobs": 1, "elapsed_s": 20}
    ) == ('P&R "demo_pnr": its stage job has left the queue (20s)')


def _guard_backend(tmp_path, monkeypatch, sbatch_body):
    """A Slurm backend whose sbatch and scancel are scripts in `tmp_path/bin`; scancel appends its argv to `tmp_path/scancel.log`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("sbatch", sbatch_body),
        ("scancel", f'echo "$*" >> {tmp_path}/scancel.log\n'),
    ):
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\n{body}")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(slurm_module, "require_tool", lambda name: None)
    monkeypatch.delenv("SBATCH_CLUSTERS", raising=False)
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())
    spec = PnrStageJobSpec(
        suite_dir=str(tmp_path),
        run_name="blk",
        run_dir=str(tmp_path),
        stage="cts",
        openroad="openroad",
        pnr_log=str(tmp_path / "pnr.log"),
        submission_path=tmp_path / "03_cts.sbatch",
    )
    return backend, spec


def test_the_guarded_sbatch_returns_sbatchs_answer(tmp_path, monkeypatch):
    backend, spec = _guard_backend(tmp_path, monkeypatch, "echo 4242\n")

    assert backend._guarded_sbatch(["sbatch"], spec=spec, abandon=lambda: False) == (
        0,
        "4242\n",
        "",
    )
    assert (tmp_path / "03_cts.sbatch").read_text() == "4242\n"
    assert not (tmp_path / "03_cts.sbatch.err").exists()
    assert not (tmp_path / "scancel.log").exists()


def test_a_given_up_sbatch_returns_at_once_and_its_job_is_cancelled(
    tmp_path, monkeypatch
):
    backend, spec = _guard_backend(tmp_path, monkeypatch, "sleep 1\necho '4242;c2'\n")
    start = time.monotonic()

    with pytest.raises(SubmissionAbandoned):
        backend._guarded_sbatch(["sbatch"], spec=spec, abandon=lambda: True)

    assert time.monotonic() - start < 0.9
    # sbatch answers later in its own session; the script cancels what it accepted.
    log = tmp_path / "scancel.log"
    deadline = time.monotonic() + 10
    while not log.exists() and time.monotonic() < deadline:
        threading.Event().wait(0.05)
    assert log.read_text() == "-M c2 4242\n"


def test_an_sbatch_that_does_not_answer_is_stopped_and_fails(tmp_path, monkeypatch):
    backend, spec = _guard_backend(tmp_path, monkeypatch, "sleep 30\necho 4242\n")
    monkeypatch.setattr(slurm_module, "SUBMIT_TIMEOUT_S", 1.0)
    start = time.monotonic()

    with pytest.raises(
        FatalRtlBuddyError,
        match=r"^sbatch failed for pnr:blk:cts \(rc=124\): sbatch did not answer "
        r"within 1s$",
    ):
        backend.submit_pnr_stage(spec)

    assert time.monotonic() - start < 10
    # The guard filed an empty answer, so the submission is resolved as failed.
    assert (tmp_path / "03_cts.sbatch").read_text() == ""
    assert not (tmp_path / "03_cts.sbatch.tmp").exists()


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
