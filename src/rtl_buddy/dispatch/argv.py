# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The ``rb`` re-entry command lines a dispatched job runs (#360).

A dispatched job is the same thing whatever launches it: ``rb _build-job``
for one suite's shared compile, ``rb _test-job`` for one (test, run_id).
Only the *transport* differs — an ``sbatch --wrap`` script on a compute
node (:mod:`.slurm`) or a plain ``subprocess.Popen`` on this host
(:mod:`.local_parallel`). The argv is therefore part of the backend-
independent dispatch contract and lives here, so a second backend cannot
drift from the first in what it actually executes.

Both forms re-invoke ``rb`` from the *same* Python environment
(``sys.executable``) and in ``--machine`` mode: the job's result travels
through its ``--result-json`` envelope, so its stdout is a log, not an
interface.
"""

import sys

from ..seed_mode import SeedMode
from .base import BuildJobSpec, TestJobSpec


def _rb_argv(spec) -> list[str]:
    """The common ``rb`` prefix, including builder selection."""
    argv = [sys.executable, "-m", "rtl_buddy", "--machine"]
    if spec.builder_mode is not None:
        argv += ["-M", spec.builder_mode]
    if spec.builder_override is not None:
        argv += ["-B", spec.builder_override]
    return argv


def build_job_argv(spec: BuildJobSpec) -> list[str]:
    """The ``rb _build-job`` invocation for one suite's shared compile."""
    argv = _rb_argv(spec)
    argv += ["_build-job", "-c", spec.test_config_path, "--share-build"]
    if spec.plan_path is not None:
        # Compile exactly the head's planned configs — no sweep re-run.
        argv += ["--plan", str(spec.plan_path)]
    if spec.result_json is not None:
        argv += ["--result-json", str(spec.result_json)]
    if spec.reg_level is not None:
        argv += ["-l", str(spec.reg_level)]
    if spec.start_level is not None:
        argv += ["-s", str(spec.start_level)]
    return argv


def test_job_argv(spec: TestJobSpec) -> list[str]:
    """The ``rb _test-job`` invocation for one (test, run_id)."""
    argv = _rb_argv(spec)
    argv += [
        "_test-job",
        spec.test_name,
        "-c",
        spec.test_config_path,
        "--result-json",
        str(spec.result_json),
    ]
    if spec.plan_path is not None:
        # Resolve this test's config from the head's plan — no sweep re-run.
        argv += ["--plan", str(spec.plan_path)]
    if spec.share_build:
        argv += ["--share-build"]
    if spec.run_id is not None:
        argv += ["--run-id", str(spec.run_id)]
    if spec.seed_mode != SeedMode.DEFAULT:
        argv += ["--seed-mode", spec.seed_mode.value]
    if spec.replay_run_id is not None:
        argv += ["--replay-run-id", str(spec.replay_run_id)]
    return argv
