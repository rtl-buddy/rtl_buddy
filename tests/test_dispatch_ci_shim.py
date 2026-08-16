"""End-to-end dispatch integration test against Slurm CLI shims (#351 P4).

Unlike the unit tests (fake backend / mocked subprocess), this drives the
*real* ``SlurmDispatchBackend`` through shim ``sbatch``/``squeue``/
``sacct``/``scancel`` executables on PATH, and a fake ``verilator`` that
emits a ``simv`` printing ``PASS``. So the real subprocess boundary, the
real array manifest/script, the real ``rb _test-job`` compile→sim→post
path, envelope collection, sacct telemetry parsing, and reservation
right-sizing all run — with no scheduler or simulator. This is the CI
job the issue calls for: "mocked sbatch/sacct shim on PATH".

Skipped where bash or the shims aren't usable (non-POSIX).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rtl_buddy.dispatch.argv import job_log_path

_REPO = Path(__file__).resolve().parent.parent
_SHIMS = _REPO / "tests" / "dispatch_shims"
_FIXTURE = _REPO / "tests" / "fixtures" / "dispatch_project"
_SWEEP_FIXTURE = _REPO / "tests" / "fixtures" / "dispatch_sweep_project"

pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="dispatch shim e2e needs a POSIX shell",
)


def _run_regression(work_dir: Path, extra_args=()):
    project = work_dir / "proj"
    shutil.copytree(_FIXTURE, project)
    env = dict(os.environ)
    env["PATH"] = f"{_SHIMS}{os.pathsep}{env['PATH']}"
    env["RB_SHIM_DB"] = str(work_dir / "jobs.db")
    env["RB_SHIM_LOG"] = str(work_dir / "jobs.log")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "rtl_buddy",
            "--machine",
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "slurm",
            *extra_args,
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
    )
    envelope = None
    for line in proc.stdout.splitlines():
        if line.startswith('{"command"'):
            envelope = json.loads(line)
    # The shim tees each _test-job's own stdout/stderr here — the likely
    # failure mode is a broken job, so surface it in the assertion context.
    job_log = work_dir / "jobs.log"
    diag = proc.stdout + proc.stderr
    if job_log.exists():
        diag += "\n--- job log ---\n" + job_log.read_text()
    return proc, envelope, project, diag


@pytest.fixture(scope="module")
def shim_run(tmp_path_factory):
    # One real regression subprocess shared by the assertions below (both
    # inspect different slices of the same byte-identical run).
    return _run_regression(tmp_path_factory.mktemp("dispatch"))


def test_shim_regression_runs_real_pipeline_to_pass(shim_run):
    proc, envelope, project, diag = shim_run
    assert proc.returncode == 0, diag
    assert envelope is not None, diag
    results = {r["name"]: r["result"] for r in envelope["payload"]["results"]}
    # Both tests ran through the real backend -> real _test-job -> fake
    # verilator's PASS simv.
    assert results == {"alpha": "PASS", "beta": "PASS"}

    # The array manifest + per-element result envelopes really exist.
    dispatch_dirs = list(project.glob("verif/blk/artefacts/*/dispatch"))
    assert dispatch_dirs, "no dispatch artefacts written"
    envelopes = list(project.glob("verif/blk/artefacts/*/dispatch/result-*.json"))
    assert len(envelopes) == 2
    one = json.loads(envelopes[0].read_text())
    assert one["result"]["results"]["result"] == "PASS"

    # Each job logged beside its own envelope, not into the head's
    # verif/blk/rtl_buddy.log (#437).
    for env in envelopes:
        assert job_log_path(env).exists(), f"no job log beside {env}"
    head_log = (project / "verif" / "blk" / "rtl_buddy.log").read_text()
    assert "command.test_job" not in head_log


def test_shim_regression_attaches_sacct_telemetry_and_advice(shim_run):
    proc, envelope, project, diag = shim_run
    assert proc.returncode == 0, diag

    # sacct telemetry (5s of a 60-min limit; 20M of 512M) travels into the
    # envelope and drives over-reservation advice.
    envelopes = list(project.glob("verif/blk/artefacts/*/dispatch/result-*.json"))
    tele = json.loads(envelopes[0].read_text())["telemetry"]
    assert tele["state"] == "COMPLETED"
    assert tele["timelimit_s"] == 3600  # TimelimitRaw 60 min -> seconds
    assert tele["max_rss_bytes"] == 20480 * 1024

    advice = envelope["payload"]["reservation_advice"]
    assert advice, "expected reservation advice from over-reserved shim jobs"
    directions = {(a["resource"], a["direction"]) for a in advice}
    assert ("time", "reduce") in directions
    assert ("mem", "reduce") in directions
    # Edit hint points at the real tests.yaml field.
    hint = advice[0]["edit_hint"]
    assert hint["path"].startswith("tests[name=")
    assert hint["path"].endswith((".time", ".mem", ".cpus"))


def test_shim_sweep_hook_runs_once_across_builds_and_arrays(tmp_path_factory):
    """The sweep hook expands exactly once — on the head — no matter how
    many sim jobs or build jobs the dispatch fans out to.

    Regression against the pre-plan-manifest behaviour where the hook ran
    in the head fan-out, again in the build job, and again in every sim
    job. This drives the *real* backend through the shims: a two-suite
    regression (two model builds) whose first suite carries a `sweep` that
    expands to three variants (an array > 1). The hook appends its pid to
    ``$RB_SWEEP_COUNTER`` on every execution, so the file must end up with
    a single line.
    """
    work = tmp_path_factory.mktemp("sweep_idem")
    project = work / "proj"
    shutil.copytree(_SWEEP_FIXTURE, project)
    counter = work / "sweep_execs.txt"

    env = dict(os.environ)
    env["PATH"] = f"{_SHIMS}{os.pathsep}{env['PATH']}"
    env["RB_SHIM_DB"] = str(work / "jobs.db")
    env["RB_SHIM_LOG"] = str(work / "jobs.log")
    env["RB_SWEEP_COUNTER"] = str(counter)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "rtl_buddy",
            "--machine",
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "slurm",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
    )
    job_log = work / "jobs.log"
    diag = proc.stdout + proc.stderr
    if job_log.exists():
        diag += "\n--- job log ---\n" + job_log.read_text()
    assert proc.returncode == 0, diag

    # (1) The idempotency invariant: exactly one hook execution, on the head.
    execs = counter.read_text().split() if counter.exists() else []
    assert len(execs) == 1, (
        f"sweep hook ran {len(execs)}x, expected 1 "
        f"(pre-plan-manifest this would be 1 head + 1 build + 3 sims = 5): "
        f"{execs}\n{diag}"
    )

    # (2) Multiple builds: one plan manifest (== one build job) per suite.
    plans = sorted(project.glob("verif/*/artefacts/.dispatch/plan-*.json"))
    assert len(plans) == 2, [str(p) for p in plans]

    # (3) Array > 1: the swept suite's array manifest holds all three variants.
    manifests = list(project.glob("verif/swept/artefacts/.dispatch/*/manifest.txt"))
    assert manifests, f"no array manifest written\n{diag}"
    elements = sum(len(m.read_text().splitlines()) for m in manifests)
    assert elements == 3, f"expected a 3-element array, got {elements}\n{diag}"

    # (4) And all three variants + the second suite's test really ran to PASS.
    envelope = None
    for line in proc.stdout.splitlines():
        if line.startswith('{"command"'):
            envelope = json.loads(line)
    assert envelope is not None, diag
    results = {r["name"]: r["result"] for r in envelope["payload"]["results"]}
    for name in ("wide_v0", "wide_v1", "wide_v2", "solo"):
        assert results.get(name) == "PASS", (name, results, diag)
