# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Slurm dispatch backend (#351 P1).

Each (test, run_id) becomes one ``sbatch --wrap`` job that re-invokes
``rb _test-job`` from the same Python environment (``sys.executable``,
which lives on the shared filesystem alongside the project). The head
process must have compiled the sim executable first with ``share_build``
so the job's own ``compile()`` short-circuits on the shared-build stamp
and the job effectively runs SIM + POST only.

Collection waits for the queue to drain via ``squeue`` polling; loading
the per-job result envelopes is the caller's job (backend-independent).

The Slurm client calls (``sbatch`` / ``squeue`` / ``scancel``) use plain
``subprocess.run`` rather than ``run_managed_process``: they are short,
synchronous probes that submit or poll and return immediately, not
long-lived simulation processes that need signal-forwarding / cleanup.
Each passes an explicit ``cwd`` per the engineering guidelines, since the
head process cwd is re-anchored per suite during a regression.
"""

import logging
import shlex
import subprocess
import sys
import time

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..seed_mode import SeedMode
from ..tool_manifest import require as require_tool
from .base import BuildJobSpec, DispatchBackend, JobHandle, TestJobSpec

logger = logging.getLogger(__name__)

# Queue states that mean "still occupying the queue". Anything else
# (COMPLETED/FAILED/TIMEOUT/CANCELLED...) has finished as far as the
# collector is concerned — the result envelope decides pass/fail.
_ACTIVE_STATES = "PD,R,S,CG,CF"


class SlurmDispatchBackend(DispatchBackend):
    name = "slurm"

    def __init__(self, dispatch_cfg):
        # Fail with the manifest's install hint, not a raw FileNotFoundError
        # from the first subprocess.run, when the Slurm client is absent.
        require_tool("slurm")
        self.sbatch_args = list(dispatch_cfg.sbatch_args)
        self.poll_interval = dispatch_cfg.poll_interval

    @staticmethod
    def _cwd_of(handles: list[JobHandle]) -> str | None:
        return handles[0].spec.suite_dir if handles else None

    def _job_argv(self, spec: TestJobSpec) -> list[str]:
        """The ``rb _test-job`` invocation the batch script runs."""
        argv = [sys.executable, "-m", "rtl_buddy", "--machine"]
        if spec.builder_mode is not None:
            argv += ["-M", spec.builder_mode]
        if spec.builder_override is not None:
            argv += ["-B", spec.builder_override]
        argv += [
            "_test-job",
            spec.test_name,
            "-c",
            spec.test_config_path,
            "--result-json",
            str(spec.result_json),
        ]
        if spec.share_build:
            argv += ["--share-build"]
        if spec.run_id is not None:
            argv += ["--run-id", str(spec.run_id)]
        if spec.seed_mode != SeedMode.DEFAULT:
            argv += ["--seed-mode", spec.seed_mode.value]
        if spec.replay_run_id is not None:
            argv += ["--replay-run-id", str(spec.replay_run_id)]
        return argv

    def _reservation_argv(self, resources, *, job_name, chdir, log_path) -> list[str]:
        """Common sbatch reservation flags shared by build and sim jobs."""
        cmd = [
            "sbatch",
            "--parsable",
            f"--job-name={job_name}",
            f"--chdir={chdir}",
            # Always explicit: right-sizing needs a defined time limit,
            # and site partitions may default to UNLIMITED.
            f"--time={resources.time}",
            f"--cpus-per-task={resources.cpus}",
        ]
        if resources.mem is not None:
            cmd.append(f"--mem={resources.mem}")
        if log_path is not None:
            # stderr merges into --output when --error is not given.
            cmd.append(f"--output={log_path}")
        return cmd

    def _build_argv(self, spec: BuildJobSpec) -> list[str]:
        """The ``rb _build-job`` invocation the build job runs."""
        argv = [sys.executable, "-m", "rtl_buddy", "--machine"]
        if spec.builder_mode is not None:
            argv += ["-M", spec.builder_mode]
        if spec.builder_override is not None:
            argv += ["-B", spec.builder_override]
        argv += ["_build-job", "-c", spec.test_config_path, "--share-build"]
        if spec.reg_level is not None:
            argv += ["-l", str(spec.reg_level)]
        if spec.start_level is not None:
            argv += ["-s", str(spec.start_level)]
        return argv

    def submit_build(self, spec: BuildJobSpec) -> JobHandle:
        cmd = self._reservation_argv(
            spec.resources,
            job_name="rb-build",
            chdir=spec.suite_dir,
            log_path=spec.log_path,
        )
        cmd += self.sbatch_args
        cmd += ["--wrap", shlex.join(self._build_argv(spec))]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=spec.suite_dir)
        if proc.returncode != 0:
            raise FatalRtlBuddyError(
                f"sbatch failed for build job (rc={proc.returncode}): "
                f"{proc.stderr.strip()}"
            )
        job_id = proc.stdout.strip().split(";")[0]
        if not job_id:
            raise FatalRtlBuddyError("sbatch returned no job id for build job")
        log_event(
            logger,
            logging.INFO,
            "dispatch.build_submitted",
            backend=self.name,
            job_id=job_id,
            suite_dir=spec.suite_dir,
            time=spec.resources.time,
            cpus=spec.resources.cpus,
            mem=spec.resources.mem,
        )
        return JobHandle(job_id=job_id, spec=spec)

    def _sbatch_argv(self, spec: TestJobSpec, dependency: str | None) -> list[str]:
        cmd = self._reservation_argv(
            spec.resources,
            job_name=f"rb:{spec.display_name()}",
            chdir=spec.suite_dir,
            log_path=spec.log_path,
        )
        if dependency is not None:
            # afterok: the sim only runs if the shared build succeeded.
            cmd.append(f"--dependency=afterok:{dependency}")
        cmd += self.sbatch_args
        cmd += ["--wrap", shlex.join(self._job_argv(spec))]
        return cmd

    def submit(self, spec: TestJobSpec, *, dependency: str | None = None) -> JobHandle:
        argv = self._sbatch_argv(spec, dependency)
        proc = subprocess.run(argv, capture_output=True, text=True, cwd=spec.suite_dir)
        if proc.returncode != 0:
            raise FatalRtlBuddyError(
                f"sbatch failed for {spec.display_name()} "
                f"(rc={proc.returncode}): {proc.stderr.strip()}"
            )
        # --parsable prints "jobid" or "jobid;cluster".
        job_id = proc.stdout.strip().split(";")[0]
        if not job_id:
            raise FatalRtlBuddyError(
                f"sbatch returned no job id for {spec.display_name()}"
            )
        log_event(
            logger,
            logging.INFO,
            "dispatch.submitted",
            backend=self.name,
            job_id=job_id,
            test=spec.test_name,
            run_id=spec.run_id,
            dependency=dependency,
            time=spec.resources.time,
            cpus=spec.resources.cpus,
            mem=spec.resources.mem,
        )
        return JobHandle(job_id=job_id, spec=spec)

    def wait_all(self, handles: list[JobHandle]) -> None:
        if not handles:
            return
        ids = ",".join(h.job_id for h in handles)
        cwd = self._cwd_of(handles)
        while True:
            proc = subprocess.run(
                [
                    "squeue",
                    "--noheader",
                    "--format=%i",
                    f"--states={_ACTIVE_STATES}",
                    "--jobs",
                    ids,
                ],
                capture_output=True,
                text=True,
                cwd=cwd,
            )
            # squeue errors ("Invalid job id specified") once every job
            # has aged out of the queue — that is completion, not failure.
            remaining = (
                [line for line in proc.stdout.split() if line]
                if proc.returncode == 0
                else []
            )
            if not remaining:
                log_event(
                    logger,
                    logging.INFO,
                    "dispatch.drained",
                    backend=self.name,
                    jobs=len(handles),
                )
                return
            log_event(
                logger,
                logging.DEBUG,
                "dispatch.waiting",
                backend=self.name,
                remaining=len(remaining),
                total=len(handles),
            )
            time.sleep(self.poll_interval)

    def cancel_all(self, handles: list[JobHandle]) -> None:
        if not handles:
            return
        subprocess.run(
            ["scancel", *(h.job_id for h in handles)],
            capture_output=True,
            text=True,
            cwd=self._cwd_of(handles),
        )
        log_event(
            logger,
            logging.WARNING,
            "dispatch.cancelled",
            backend=self.name,
            jobs=len(handles),
        )
