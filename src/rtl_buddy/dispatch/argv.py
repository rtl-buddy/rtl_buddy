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
from .base import BuildJobSpec, ElabJobSpec, RunnableJobSpec, TestJobSpec


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
    # Beside --share-build because it qualifies it: the flag says "share a
    # build", this says where that build lives (#542). Omitted entirely when
    # no cache root is configured, so every existing project's argv — and
    # every job script diff — is unchanged.
    if spec.shared_build_root is not None:
        argv += ["--shared-build-root", str(spec.shared_build_root)]
    if spec.parallel > 1:
        # Omitted at the default: an argv byte-identical to a pre-#495
        # head's keeps plan/manifest and job-script diffs quiet for every
        # project that never asks for concurrency.
        argv += ["--parallel", str(spec.parallel)]
    if (
        spec.parallel_configured is not None
        and spec.parallel_configured != spec.parallel
    ):
        # Only when the plan's cap actually bit (#547 review). Everywhere
        # else the configured value IS `--parallel`, and restating it would
        # change the job script of every project that sets `compile.parallel`
        # for no diagnostic gain. The job needs it to name the number the
        # config file holds rather than the capped one it was handed.
        argv += ["--parallel-configured", str(spec.parallel_configured)]
    if spec.rebuild:
        # Omitted at the default, like --parallel above: an unchanged argv
        # keeps job-script diffs quiet for every run that did not ask.
        argv += ["--rebuild"]
    if spec.plan_path is not None:
        # Compile exactly the head's planned configs — no sweep re-run.
        argv += ["--plan", str(spec.plan_path)]
    if spec.result_json is not None:
        argv += ["--result-json", str(spec.result_json)]
    if spec.gates_json is not None:
        # Absent for every backend that cannot release a pending job, so
        # their build job's argv is unchanged (#548).
        argv += ["--gates", str(spec.gates_json)]
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
    # Same pairing as the build job's: a simulation job that resolved a
    # different cache root would look for the build in a directory the
    # build job never wrote (#542).
    if spec.shared_build_root is not None:
        argv += ["--shared-build-root", str(spec.shared_build_root)]
    if spec.expect_prebuilt:
        argv += ["--expect-prebuilt"]
    if spec.rebuild:
        argv += ["--rebuild"]
    if spec.build_result_json is not None:
        # Only ever set alongside --expect-prebuilt: without a build job
        # there is no envelope, and an ungated job has nothing to consult.
        # Absent for every non-gated job, so their argv is unchanged (#498).
        argv += ["--build-result-json", str(spec.build_result_json)]
    if spec.run_id is not None:
        argv += ["--run-id", str(spec.run_id)]
    if spec.seed_mode != SeedMode.DEFAULT:
        argv += ["--seed-mode", spec.seed_mode.value]
    if spec.replay_run_id is not None:
        argv += ["--replay-run-id", str(spec.replay_run_id)]
    if spec.master_seed is not None:
        argv += ["--master-seed", str(spec.master_seed)]
    if spec.resolved_seed is not None:
        argv += ["--resolved-seed", str(spec.resolved_seed)]
    return argv


def elab_job_argv(spec: ElabJobSpec) -> list[str]:
    """The ``rb _elab-job`` invocation for one model/profile pair."""
    argv = _rb_argv(spec)
    argv += [
        "_elab-job",
        spec.model_name,
        "-c",
        spec.model_config_path,
        "--result-json",
        str(spec.result_json),
        "--cpus",
        str(spec.resources.cpus),
    ]
    if spec.profile_name is not None:
        argv += ["--profile", spec.profile_name]
    return argv


def job_argv(spec: RunnableJobSpec) -> list[str]:
    if isinstance(spec, ElabJobSpec):
        return elab_job_argv(spec)
    return test_job_argv(spec)
