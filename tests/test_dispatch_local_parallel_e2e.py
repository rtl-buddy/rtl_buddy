"""End-to-end ``--dispatch local-parallel`` regression (#360).

The counterpart of :mod:`test_dispatch_ci_shim` for the native-process
backend: the same fixture project and the same fake ``verilator``, but with
**no scheduler shims in play** — the point of this backend is that a
laptop needs none. So the real pool launches real ``rb _build-job`` /
``rb _test-job`` subprocesses, gates the sims on the build, collects their
envelopes, and the run must reach PASS while never invoking ``sbatch``.

The Slurm shim directory is still on PATH (that is where the fake
``verilator`` lives), which makes "the scheduler was never called" a
falsifiable assertion rather than a tautology: the shims record every
invocation in ``$RB_SHIM_DB``, so that file must not exist afterwards.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SHIMS = _REPO / "tests" / "dispatch_shims"
_FIXTURE = _REPO / "tests" / "fixtures" / "dispatch_project"
_SWEEP_FIXTURE = _REPO / "tests" / "fixtures" / "dispatch_sweep_project"
_PARALLEL_FIXTURE = _REPO / "tests" / "fixtures" / "dispatch_parallel_project"

pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="the fake verilator these fixtures build with needs a POSIX shell",
)


def _run(
    work_dir: Path,
    fixture: Path,
    extra_args=(),
    extra_env=None,
    command=("regression", "-c", "regression.yaml"),
    cwd_rel=".",
):
    project = work_dir / "proj"
    shutil.copytree(fixture, project)
    env = dict(os.environ)
    # The fake verilator lives beside the Slurm shims; RB_SHIM_DB is where
    # those shims would announce themselves if anything called them.
    env["PATH"] = f"{_SHIMS}{os.pathsep}{env['PATH']}"
    env["RB_SHIM_DB"] = str(work_dir / "jobs.db")
    env["RB_SHIM_LOG"] = str(work_dir / "jobs.log")
    env.update(extra_env or {})
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "rtl_buddy",
            "--machine",
            *command,
            "--dispatch",
            "local-parallel",
            *extra_args,
        ],
        cwd=project / cwd_rel,
        capture_output=True,
        text=True,
        env=env,
    )
    envelope = None
    for line in proc.stdout.splitlines():
        if line.startswith('{"command"'):
            envelope = json.loads(line)
    diag = proc.stdout + proc.stderr
    # A broken job is the likely failure, and its output is in its own log.
    for log in sorted(project.glob("verif/*/artefacts/*/dispatch/*.log")) + sorted(
        project.glob("verif/*/artefacts/.dispatch/build-*.log")
    ):
        diag += f"\n--- {log.name} ---\n" + log.read_text()
    return proc, envelope, project, diag


@pytest.fixture(scope="module")
def pool_run(tmp_path_factory):
    # One real regression subprocess shared by the assertions below.
    work = tmp_path_factory.mktemp("local_parallel")
    return (*_run(work, _FIXTURE, extra_args=("-j", "2")), work)


def test_pool_regression_runs_the_real_pipeline_to_pass(pool_run):
    proc, envelope, project, diag, _work = pool_run
    assert proc.returncode == 0, diag
    assert envelope is not None, diag
    results = {r["name"]: r["result"] for r in envelope["payload"]["results"]}
    assert results == {"alpha": "PASS", "beta": "PASS"}

    # Real per-job envelopes, written by real subprocesses.
    envelopes = list(project.glob("verif/blk/artefacts/*/dispatch/result-*.json"))
    assert len(envelopes) == 2, diag
    assert json.loads(envelopes[0].read_text())["result"]["results"]["result"] == "PASS"

    # Each job's stdout landed in its own log, named for the backend that
    # wrote it — and the head's machine-mode stdout stayed parseable (the
    # envelope above), which inherited stdout would have corrupted.
    logs = sorted(
        p.name
        for p in project.glob("verif/blk/artefacts/*/dispatch/local-parallel-*.log")
    )
    assert logs == ["local-parallel-single.log", "local-parallel-single.log"], diag
    assert list(project.glob("verif/blk/artefacts/.dispatch/build-*.log")) != [], diag


def test_pool_regression_never_calls_the_scheduler(pool_run):
    proc, _envelope, _project, diag, work = pool_run
    assert proc.returncode == 0, diag
    # The shims are on PATH and record every call; nothing called them.
    assert not (work / "jobs.db").exists(), "a Slurm CLI was invoked"
    assert not (work / "jobs.log").exists(), "a Slurm CLI was invoked"


def test_pool_regression_reports_no_reservation_advice(pool_run):
    proc, envelope, _project, diag, _work = pool_run
    assert proc.returncode == 0, diag
    # No accounting source, so right-sizing degrades to no advice rather
    # than inventing utilization numbers (the documented non-goal).
    assert envelope["payload"]["reservation_advice"] == [], diag


def test_pool_expands_the_sweep_once_across_build_and_sim_jobs(tmp_path_factory):
    """The plan-manifest invariant holds on this backend too.

    Same property as the Slurm shim test: the sweep hook runs exactly once,
    on the head — not again in the build job and once per sim job.
    """
    work = tmp_path_factory.mktemp("local_parallel_sweep")
    counter = work / "sweep_execs.txt"
    proc, envelope, project, diag = _run(
        work,
        _SWEEP_FIXTURE,
        extra_args=("-j", "3"),
        extra_env={"RB_SWEEP_COUNTER": str(counter)},
    )
    assert proc.returncode == 0, diag

    execs = counter.read_text().split() if counter.exists() else []
    assert len(execs) == 1, f"sweep hook ran {len(execs)}x, expected 1: {execs}\n{diag}"

    # One plan per suite (two suites, two build jobs).
    plans = sorted(project.glob("verif/*/artefacts/.dispatch/plan-*.json"))
    assert len(plans) == 2, [str(p) for p in plans]

    assert envelope is not None, diag
    results = {r["name"]: r["result"] for r in envelope["payload"]["results"]}
    for name in ("wide_v0", "wide_v1", "wide_v2", "solo"):
        assert results.get(name) == "PASS", (name, results, diag)


def test_pool_runs_a_single_test_from_its_suite_dir(tmp_path_factory):
    """`rb test <name> --dispatch` end to end, with real subprocesses (#440).

    The one thing the fake-backend tests cannot show: a real `rb _build-job`
    and a real `rb _test-job` for a *single* named test, launched from the
    suite directory the way anyone iterating on one failing test would. The
    suite's other test must not be dragged along — that whole-suite cost is
    what the throwaway-reg_config workaround charged.
    """
    work = tmp_path_factory.mktemp("local_parallel_single_test")
    proc, envelope, project, diag = _run(
        work,
        _FIXTURE,
        # `-M reg`: the fixture builder only declares a `reg` opts block,
        # and `rb test` defaults the builder mode to `debug` (this is the
        # unchanged default, not something dispatch alters).
        command=("-M", "reg", "test", "alpha"),
        cwd_rel="verif/blk",
    )
    assert proc.returncode == 0, diag
    assert envelope is not None, diag
    assert [(r["name"], r["result"]) for r in envelope["payload"]["results"]] == [
        ("alpha", "PASS")
    ], diag

    # Exactly one sim job's envelope — "beta" was never planned, never built
    # for, and never run.
    envelopes = sorted(project.glob("verif/blk/artefacts/*/dispatch/result-*.json"))
    assert [p.parent.parent.name for p in envelopes] == ["alpha"], diag
    # ...gated on a real build job, which left its own log.
    assert list(project.glob("verif/blk/artefacts/.dispatch/build-*.log")) != [], diag
    # No scheduler was involved on this backend.
    assert not (work / "jobs.db").exists(), "a Slurm CLI was invoked"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - reparented and not ours
        return True
    return True


def _recorded_compiler_pids(pids_file: Path) -> list[int]:
    if not pids_file.exists():
        return []
    return [int(tok) for tok in pids_file.read_text().split() if tok.strip().isdigit()]


def _start_hanging_build_job(project: Path, pids_file: Path, parallel: str):
    env = dict(os.environ)
    env["PATH"] = f"{_SHIMS}{os.pathsep}{env['PATH']}"
    # Long enough that "it died" cannot be confused with "it finished".
    env["RB_SHIM_HANG"] = "60"
    env["RB_SHIM_PIDS"] = str(pids_file)
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "rtl_buddy",
            "_build-job",
            "-c",
            "tests.yaml",
            "--parallel",
            parallel,
        ],
        cwd=project / "verif" / "blk",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_cancelling_a_parallel_build_job_kills_its_compilers(tmp_path_factory):
    """SIGTERM to the build job must take the compilers with it (#496 review).

    This is what ``local-parallel``'s ``cancel_all`` does on Ctrl-C or
    ``--max-wait``: it signals the ``rb _build-job`` process group and nothing
    else. The compilers are not in that group — a worker thread started them
    and ``run_managed_process`` gives every child its own session — so before
    #495's sweeper this killed the job and left two Verilations burning the
    node until they finished. A real ``rb _build-job`` subprocess over the
    two-compile-key fixture, with the fake verilator parked in a 60 s sleep,
    is the only place that is observable end to end.
    """
    work = tmp_path_factory.mktemp("build_job_cancel")
    project = work / "proj"
    shutil.copytree(_PARALLEL_FIXTURE, project)
    pids_file = work / "compiler_pids.txt"
    proc = _start_hanging_build_job(project, pids_file, "2")
    compilers: list[int] = []
    try:
        # Both compilers in flight: the pool is what is being cancelled, so
        # cancelling before it filled would prove nothing.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            compilers = _recorded_compiler_pids(pids_file)
            if len(compilers) >= 2:
                break
            time.sleep(0.1)
        assert len(compilers) >= 2, f"compilers never started: {compilers}"

        proc.send_signal(signal.SIGTERM)
        out = proc.communicate(timeout=60)[0]
        # The handler's re-raise convention, straight from run_managed_process.
        assert proc.returncode == 128 + signal.SIGTERM, out

        # ...and the grandchildren are gone. Polled, because they are reaped
        # by init after the job exits, not by this process.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and any(_pid_alive(pid) for pid in compilers):
            time.sleep(0.1)
        assert [pid for pid in compilers if _pid_alive(pid)] == [], out
        # None of them reached the far side of its sleep.
        assert "survived" not in pids_file.read_text(), out
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a failed run
            proc.kill()
            proc.communicate()
        for pid in compilers:  # pragma: no cover - only on a failed run
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass


def _append_third_compile_key(project: Path) -> None:
    """A third test with its own plusdefine, i.e. a third distinct build."""
    tests_yaml = project / "verif" / "blk" / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text()
        + """  - name: gamma
    desc: dispatch ci test gamma, a third compile key queued behind the pool
    model: m
    model_path: models.yaml
    reglvl: 0
    plusargs:
    plusdefines:
      WIDTH: 16
    uvm:
    preproc:
    postproc:
    sweep:
    testbench: tb_blk
    sim_timeout:
"""
    )


def test_cancelling_a_parallel_build_job_starts_no_queued_compiler(tmp_path_factory):
    """No compiler may start after cancellation began (#496 review).

    Three compile keys, two pool slots, so group 3 is still queued when the
    SIGTERM arrives; the fake verilator's pid file records everything that
    ever launched, and a third pid in it is a compiler started after the
    sweep — in its own session, with the job already on its way out, so the
    local backend's 5 s grace kills the wrapper and leaves it running.

    The end-to-end guard, not the regression proof: whether that third
    compiler starts is a race between the swept worker taking group 3 and
    the main thread unwinding (``Executor.map``'s result generator cancels
    what is still pending as it closes), and on an idle machine the unwind
    usually wins even without the latch. The deterministic version of this
    is ``test_a_cancelled_build_job_never_starts_a_queued_group`` in
    ``test_test_job.py``, which puts the worker on the losing side of that
    race by construction.
    """
    work = tmp_path_factory.mktemp("build_job_cancel_queued")
    project = work / "proj"
    shutil.copytree(_PARALLEL_FIXTURE, project)
    _append_third_compile_key(project)
    pids_file = work / "compiler_pids.txt"
    proc = _start_hanging_build_job(project, pids_file, "2")
    compilers: list[int] = []
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            compilers = _recorded_compiler_pids(pids_file)
            if len(compilers) >= 2:
                break
            time.sleep(0.1)
        assert len(compilers) >= 2, f"compilers never started: {compilers}"

        proc.send_signal(signal.SIGTERM)
        out = proc.communicate(timeout=60)[0]
        assert proc.returncode == 128 + signal.SIGTERM, out

        # The pool slots were two, so a third pid here is a compiler that was
        # launched *after* cancellation began — the orphan this closes. The
        # job has exited by now, so the file is final.
        after = _recorded_compiler_pids(pids_file)
        assert len(after) == 2, f"a queued worker launched a compiler: {after}\n{out}"

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and any(_pid_alive(pid) for pid in after):
            time.sleep(0.1)
        assert [pid for pid in after if _pid_alive(pid)] == [], out
        assert "survived" not in pids_file.read_text(), out
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a failed run
            proc.kill()
            proc.communicate()
        for pid in _recorded_compiler_pids(pids_file):  # pragma: no cover
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
