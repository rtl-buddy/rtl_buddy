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

:func:`job_log_path` lives here for the same reason: the head passes
``--result-json`` in the argv above and the job reads it back out, so the
one path both sides must agree on is derived here, next to the argv that
carries it — not once in the head's submit code and again in the command
handler, where the two could drift into different files.
"""

import sys
from pathlib import Path

from ..seed_mode import SeedMode
from .base import BuildJobSpec, TestJobSpec


def job_log_path(result_json: str | Path) -> Path:
    """The rtl_buddy file log of the job that writes ``result_json``, beside it.

    A dispatched job must not write the head's ``<suite>/rtl_buddy.log``:
    both processes would open the same path and the first open of a path
    in a process truncates it, so the jobs and the head overwrite each
    other's records (#437). Each job therefore logs beside its own result
    envelope, named after it so the pair is obvious in the directory:

    - ``…/dispatch/result-<tag>.json`` → ``…/dispatch/rtl_buddy-<tag>.log``
    - ``…/.dispatch/build-result-<pid>.json``
      → ``…/.dispatch/build-rtl_buddy-<pid>.log``
    - anything else (``foo.json``) → ``…/rtl_buddy-foo.log``

    The result sits alongside the scheduler's own stdout log for the same
    job (``slurm-<tag>.log`` / ``local-parallel-<tag>.log``,
    ``build-<pid>.log``). Relative input yields a relative path.
    """
    path = Path(result_json)
    stem = path.stem
    if stem.startswith("build-result-"):
        name = f"build-rtl_buddy-{stem[len('build-result-') :]}.log"
    elif stem.startswith("result-"):
        name = f"rtl_buddy-{stem[len('result-') :]}.log"
    else:
        name = f"rtl_buddy-{stem}.log"
    return path.parent / name


def _rb_argv(spec) -> list[str]:
    """The common ``rb`` prefix, including builder selection."""
    argv = [sys.executable, "-m", "rtl_buddy", "--machine"]
    if spec.builder_mode is not None:
        argv += ["-M", spec.builder_mode]
    if spec.builder_override is not None:
        argv += ["-B", spec.builder_override]
    # A child re-reads root_config.yaml, so a builder's own extra-sim-timeout
    # survives without help; the CLI override does not, and dropping it here
    # silently ignores --extra-sim-timeout for every dispatched sim, including
    # ``--extra-sim-timeout 0`` whose whole purpose is turning a configured
    # allowance off. Build jobs never reach SIM and ignore it.
    if spec.extra_sim_timeout is not None:
        argv += ["--extra-sim-timeout", str(spec.extra_sim_timeout)]
    return argv


def build_job_argv(spec: BuildJobSpec) -> list[str]:
    """The ``rb _build-job`` invocation for one suite's shared compile."""
    argv = _rb_argv(spec)
    argv += ["_build-job", "-c", spec.test_config_path, "--share-build"]
    if spec.parallel > 1:
        # Omitted at the default: an argv byte-identical to a pre-#495
        # head's keeps plan/manifest and job-script diffs quiet for every
        # project that never asks for concurrency.
        argv += ["--parallel", str(spec.parallel)]
    if spec.rebuild:
        # Omitted at the default, like --parallel above: an unchanged argv
        # keeps job-script diffs quiet for every run that did not ask.
        argv += ["--rebuild"]
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
    if spec.expect_prebuilt:
        argv += ["--expect-prebuilt"]
    if spec.rebuild:
        argv += ["--rebuild"]
    if spec.run_id is not None:
        argv += ["--run-id", str(spec.run_id)]
    if spec.seed_mode != SeedMode.DEFAULT:
        argv += ["--seed-mode", spec.seed_mode.value]
    if spec.replay_run_id is not None:
        argv += ["--replay-run-id", str(spec.replay_run_id)]
    return argv
