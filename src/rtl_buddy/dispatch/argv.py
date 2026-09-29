# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The ``rb`` command lines that dispatched jobs run, shared by every backend."""

import sys
from pathlib import Path

from ..seed_mode import SeedMode
from .base import (
    BUILD_PHASE_FULL,
    BuildJobSpec,
    ElabJobSpec,
    RunnableJobSpec,
    TestJobSpec,
)


def job_log_path(result_json: str | Path) -> Path:
    """The rtl_buddy file log of the job that writes ``result_json``, beside it.

    A job must not write the head's ``<suite>/rtl_buddy.log``. Mapping:

    - ``…/dispatch/result-<tag>.json`` → ``…/dispatch/rtl_buddy-<tag>.log``
    - ``…/.dispatch/build-result-<pid>.json``
      → ``…/.dispatch/build-rtl_buddy-<pid>.log``
    - ``…/.dispatch/verilate-result-<pid>.json``
      → ``…/.dispatch/verilate-rtl_buddy-<pid>.log``
    - anything else (``foo.json``) → ``…/rtl_buddy-foo.log``
    """
    path = Path(result_json)
    stem = path.stem
    if stem.startswith("build-result-"):
        name = f"build-rtl_buddy-{stem[len('build-result-') :]}.log"
    elif stem.startswith("verilate-result-"):
        name = f"verilate-rtl_buddy-{stem[len('verilate-result-') :]}.log"
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
    # The CLI override must be forwarded, including 0, which disables a configured allowance.
    if spec.extra_sim_timeout is not None:
        argv += ["--extra-sim-timeout", str(spec.extra_sim_timeout)]
    return argv


def build_job_argv(spec: BuildJobSpec) -> list[str]:
    """The ``rb _build-job`` invocation for one suite's shared compile."""
    argv = _rb_argv(spec)
    argv += ["_build-job", "-c", spec.test_config_path, "--share-build"]
    if spec.phase != BUILD_PHASE_FULL:
        # Omitted at the default so unsplit suites keep their argv.
        argv += ["--phase", spec.phase]
    # An empty value is not the same as absent: it means the head was told not to cache.
    if spec.shared_build_root is not None:
        argv += ["--shared-build-root", str(spec.shared_build_root)]
    if spec.parallel > 1:
        # Omitted at the default so job scripts stay unchanged.
        argv += ["--parallel", str(spec.parallel)]
    if (
        spec.parallel_configured is not None
        and spec.parallel_configured != spec.parallel
    ):
        # Only when the plan capped the value, so the job can name the configured number.
        argv += ["--parallel-configured", str(spec.parallel_configured)]
    if spec.rebuild:
        argv += ["--rebuild"]
    if spec.plan_path is not None:
        # Compile the head's planned configs; no sweep re-run.
        argv += ["--plan", str(spec.plan_path)]
    if spec.result_json is not None:
        argv += ["--result-json", str(spec.result_json)]
    if spec.gates_json is not None:
        # Absent for backends that cannot release a pending job.
        argv += ["--gates", str(spec.gates_json)]
    if spec.reg_level is not None:
        argv += ["-l", str(spec.reg_level)]
    if spec.start_level is not None:
        argv += ["-s", str(spec.start_level)]
    if spec.run_tag is not None:
        # The job must compute the same artefact tree the head planned.
        argv += ["--run-tag", spec.run_tag]
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
        # Resolve the config from the head's plan; no sweep re-run.
        argv += ["--plan", str(spec.plan_path)]
    if spec.share_build:
        argv += ["--share-build"]
    # Must match the build job's root, or the sim looks in the wrong directory.
    if spec.shared_build_root is not None:
        argv += ["--shared-build-root", str(spec.shared_build_root)]
    if spec.expect_prebuilt:
        argv += ["--expect-prebuilt"]
    if spec.rebuild:
        argv += ["--rebuild"]
    if spec.build_result_json is not None:
        # Set only alongside --expect-prebuilt.
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
    for key, value in (spec.plusarg_overrides or {}).items():
        # Valueless keys stay valueless so the job's parse yields the same dict.
        argv += ["--plusarg", key if value is None else f"{key}={value}"]
    if spec.run_tag is not None:
        argv += ["--run-tag", spec.run_tag]
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
