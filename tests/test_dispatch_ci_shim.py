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

_REPO = Path(__file__).resolve().parent.parent
_SHIMS = _REPO / "tests" / "dispatch_shims"
_FIXTURE = _REPO / "tests" / "fixtures" / "dispatch_project"

pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="dispatch shim e2e needs a POSIX shell",
)


def _run_regression(tmp_path: Path, extra_args=()):
    project = tmp_path / "proj"
    shutil.copytree(_FIXTURE, project)
    env = dict(os.environ)
    env["PATH"] = f"{_SHIMS}{os.pathsep}{env['PATH']}"
    env["RB_SHIM_DB"] = str(tmp_path / "jobs.db")
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
    return proc, envelope, project


def test_shim_regression_runs_real_pipeline_to_pass(tmp_path: Path):
    proc, envelope, project = _run_regression(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert envelope is not None, proc.stdout
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


def test_shim_regression_attaches_sacct_telemetry_and_advice(tmp_path: Path):
    proc, envelope, project = _run_regression(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr

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
