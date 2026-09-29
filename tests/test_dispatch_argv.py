# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The ``rb`` re-entry argv a dispatched job runs.

``_rb_argv`` is the backend-independent dispatch contract, so a global the head accepts but does not forward is silently ignored by every dispatched job.
"""

from __future__ import annotations

from pathlib import Path

from rtl_buddy.dispatch.argv import build_job_argv, job_log_path

# Aliased so pytest does not collect them as a test function and a test class.
from rtl_buddy.dispatch.argv import test_job_argv as sim_job_argv
from rtl_buddy.dispatch.base import BuildJobSpec
from rtl_buddy.dispatch.base import TestJobSpec as SimJobSpec


def _test_spec(**kwargs) -> SimJobSpec:
    return SimJobSpec(
        test_name="alpha",
        suite_dir="/proj/verif/blk",
        test_config_path="/proj/verif/blk/tests.yaml",
        result_json=Path("/proj/verif/blk/artefacts/.dispatch/r.json"),
        **kwargs,
    )


def _build_spec(**kwargs) -> BuildJobSpec:
    return BuildJobSpec(
        suite_dir="/proj/verif/blk",
        test_config_path="/proj/verif/blk/tests.yaml",
        **kwargs,
    )


def _flag_value(argv, flag):
    """The value following ``flag``, or None when the flag is absent."""
    return argv[argv.index(flag) + 1] if flag in argv else None


def test_extra_sim_timeout_is_forwarded_to_a_sim_job():
    argv = sim_job_argv(_test_spec(extra_sim_timeout=900))
    assert _flag_value(argv, "--extra-sim-timeout") == "900"


def test_master_and_resolved_seeds_are_forwarded_to_a_sim_job():
    argv = sim_job_argv(_test_spec(master_seed=20260914, resolved_seed=410729))
    assert _flag_value(argv, "--master-seed") == "20260914"
    assert _flag_value(argv, "--resolved-seed") == "410729"


def test_seed_plan_flags_are_absent_when_unset():
    argv = sim_job_argv(_test_spec())
    assert "--master-seed" not in argv
    assert "--resolved-seed" not in argv


def test_extra_sim_timeout_absent_when_unset():
    assert "--extra-sim-timeout" not in sim_job_argv(_test_spec())


def test_extra_sim_timeout_zero_is_forwarded_not_dropped():
    """``--extra-sim-timeout 0`` is forwarded; a truthiness test would drop it and leave the configured allowance running."""
    argv = sim_job_argv(_test_spec(extra_sim_timeout=0))
    assert _flag_value(argv, "--extra-sim-timeout") == "0"


def test_extra_sim_timeout_is_forwarded_to_a_build_job():
    """Build jobs never reach SIM, but the prefix is shared, so the flag appears."""
    argv = build_job_argv(_build_spec(extra_sim_timeout=900))
    assert _flag_value(argv, "--extra-sim-timeout") == "900"


def test_compile_parallel_is_forwarded_to_a_build_job():
    """The head's compile concurrency reaches the job through argv."""
    argv = build_job_argv(_build_spec(parallel=4))
    assert _flag_value(argv, "--parallel") == "4"
    assert argv.index("--parallel") > argv.index("_build-job")


def test_compile_parallel_is_absent_at_the_default():
    """At the default of 1, `--parallel` is absent from the argv."""
    assert "--parallel" not in build_job_argv(_build_spec())
    assert "--parallel" not in build_job_argv(_build_spec(parallel=1))


def test_the_configured_parallel_rides_along_when_the_plan_capped_it():
    """The job can name the number the config file holds.

    The head takes ``min(compile.parallel, planned configs)``, so a job's ``--parallel 2`` may be a capped value; the pre-cap value travels too so console lines quote ``compile.parallel`` correctly.
    """
    argv = build_job_argv(_build_spec(parallel=2, parallel_configured=4))
    assert _flag_value(argv, "--parallel-configured") == "4"
    assert argv.index("--parallel-configured") > argv.index("_build-job")


def test_the_configured_parallel_is_absent_when_the_cap_did_not_bite():
    """When the cap did not bite, the configured value is not restated."""
    assert "--parallel-configured" not in build_job_argv(_build_spec())
    assert "--parallel-configured" not in build_job_argv(
        _build_spec(parallel=4, parallel_configured=4)
    )
    # A cap that collapsed the pool to 1 omits `--parallel` but carries the configured value.
    argv = build_job_argv(_build_spec(parallel=1, parallel_configured=4))
    assert "--parallel" not in argv
    assert _flag_value(argv, "--parallel-configured") == "4"


def test_compile_parallel_is_not_a_sim_job_flag():
    """A sim job compiles at most one thing, so the flag is not forwarded."""
    assert "--parallel" not in sim_job_argv(_test_spec())


def test_build_result_json_is_forwarded_to_a_gated_sim_job():
    """A gated sim job is told where the build recorded its verdict.

    Without it the job cannot tell a failed build compile from a stale stamp, and retries both.
    """
    argv = sim_job_argv(
        _test_spec(
            expect_prebuilt=True,
            build_result_json=Path("/proj/verif/blk/artefacts/.dispatch/b.json"),
        )
    )
    assert _flag_value(argv, "--build-result-json") == (
        "/proj/verif/blk/artefacts/.dispatch/b.json"
    )
    assert argv.index("--build-result-json") > argv.index("_test-job")


def test_build_result_json_is_absent_for_an_ungated_job():
    """An ungated job has no build envelope to consult."""
    assert "--build-result-json" not in sim_job_argv(_test_spec())
    assert "--build-result-json" not in sim_job_argv(_test_spec(expect_prebuilt=True))


def test_build_result_json_is_not_a_build_job_flag():
    """The build job writes the envelope, so it is not given the flag."""
    assert "--build-result-json" not in build_job_argv(_build_spec())


def test_builder_globals_precede_the_subcommand():
    """Globals sit before ``_test-job``, or Typer rejects them."""
    argv = sim_job_argv(
        _test_spec(builder_override="vcs", builder_mode="reg", extra_sim_timeout=900)
    )
    sub = argv.index("_test-job")
    for flag in ("-B", "-M", "--extra-sim-timeout"):
        assert argv.index(flag) < sub, f"{flag} must precede the subcommand"


def test_job_log_path_pairs_a_sim_envelope():
    """A sim job's log sits beside its envelope, named after it."""
    assert job_log_path(
        Path("/proj/verif/blk/artefacts/alpha/dispatch/result-0003.json")
    ) == Path("/proj/verif/blk/artefacts/alpha/dispatch/rtl_buddy-0003.log")
    assert job_log_path(
        Path("/proj/verif/blk/artefacts/alpha/dispatch/result-single.json")
    ) == Path("/proj/verif/blk/artefacts/alpha/dispatch/rtl_buddy-single.log")


def test_job_log_path_pairs_a_build_envelope():
    """``build-result-<pid>`` keeps its ``build-`` prefix, so a build job's log is distinct in the shared .dispatch dir."""
    assert job_log_path(
        Path("/proj/verif/blk/artefacts/.dispatch/build-result-4711.json")
    ) == Path("/proj/verif/blk/artefacts/.dispatch/build-rtl_buddy-4711.log")


def test_job_log_path_keeps_a_colocated_suite_namespace():
    namespace = Path("/proj/verif/blk/artefacts/.dispatch/other-tests-a1b2c3d4e5f6")
    assert job_log_path(namespace / "build-result-4711.json") == (
        namespace / "build-rtl_buddy-4711.log"
    )


def test_job_log_path_falls_back_to_the_stem():
    """A hand-written envelope name still gets a paired log."""
    assert job_log_path(Path("/tmp/foo.json")) == Path("/tmp/rtl_buddy-foo.log")


def test_job_log_path_accepts_str_and_keeps_relative_input_relative():
    """The helper only renames; resolution belongs to the caller."""
    assert job_log_path("dispatch/result-0001.json") == Path(
        "dispatch/rtl_buddy-0001.log"
    )
    out = job_log_path("res.json")
    assert isinstance(out, Path)
    assert out == Path("rtl_buddy-res.log")
    assert not out.is_absolute()


def test_rebuild_is_forwarded_to_a_build_job():
    """The head's ``--rebuild`` reaches the build job, the one process that may act on it for a suite."""
    argv = build_job_argv(_build_spec(rebuild=True))
    assert "--rebuild" in argv
    assert argv.index("--rebuild") > argv.index("_build-job")


def test_rebuild_is_forwarded_to_a_sim_job():
    """A suite with no build job carries ``--rebuild`` on the sim elements instead."""
    argv = sim_job_argv(_test_spec(rebuild=True))
    assert "--rebuild" in argv
    assert argv.index("--rebuild") > argv.index("_test-job")


def test_rebuild_is_absent_at_the_default():
    """``--rebuild`` is absent unless asked for."""
    assert "--rebuild" not in build_job_argv(_build_spec())
    assert "--rebuild" not in sim_job_argv(_test_spec())


def test_gates_manifest_is_forwarded_to_a_build_job():
    """The build job is told where to find the head's job-id map."""
    argv = build_job_argv(_build_spec(gates_json=Path("/w/.dispatch/gates-7.json")))
    assert _flag_value(argv, "--gates") == "/w/.dispatch/gates-7.json"
    assert argv.index("--gates") > argv.index("_build-job")


def test_gates_manifest_is_absent_when_the_backend_cannot_release():
    """A backend that cannot release gets no manifest flag, as with ``local-parallel``."""
    assert "--gates" not in build_job_argv(_build_spec())
    assert BuildJobSpec(suite_dir=".", test_config_path="tests.yaml").gates_json is None


def test_the_shared_build_root_reaches_both_job_kinds():
    """The build job and its gated sim jobs derive one build directory.

    The shared build root is forwarded to both, or the build would land where no simulation looks.
    """
    root = "/shared/nfs/rb-build-cache"
    assert "--shared-build-root" in build_job_argv(_build_spec(shared_build_root=root))
    build = build_job_argv(_build_spec(shared_build_root=root))
    assert build[build.index("--shared-build-root") + 1] == root
    sim = sim_job_argv(_test_spec(shared_build_root=root))
    assert sim[sim.index("--shared-build-root") + 1] == root
    # The root sits beside the flag it qualifies.
    assert build[build.index("--share-build") + 1] == "--shared-build-root"
    assert sim[sim.index("--share-build") + 1] == "--shared-build-root"


def test_no_shared_build_root_leaves_both_argvs_untouched():
    """Without a cache root, neither argv changes."""
    assert "--shared-build-root" not in build_job_argv(_build_spec())
    assert "--shared-build-root" not in sim_job_argv(_test_spec())
    assert "--shared-build-root" not in sim_job_argv(_test_spec(share_build=False))


def test_an_explicit_disable_is_forwarded_as_an_empty_argument():
    """`None` and `""` differ: an explicit disable is forwarded as an empty argument.

    A job told nothing re-resolves the root from its environment and config, so forwarding `None` would disable only the head.
    """
    build = build_job_argv(_build_spec(shared_build_root=""))
    sim = sim_job_argv(_test_spec(shared_build_root=""))
    assert build[build.index("--shared-build-root") + 1] == ""
    assert sim[sim.index("--shared-build-root") + 1] == ""
    # An explicit disable is distinguishable from saying nothing.
    assert "--shared-build-root" not in build_job_argv(_build_spec())
    assert "--shared-build-root" not in sim_job_argv(_test_spec())


def test_the_build_phase_is_absent_at_the_default():
    """An unsplit suite has no build-phase flag."""
    assert "--phase" not in build_job_argv(_build_spec())
    assert "--phase" not in build_job_argv(_build_spec(phase="full"))


def test_each_half_of_a_split_compile_names_its_phase():
    for phase in ("verilate", "build"):
        argv = build_job_argv(_build_spec(phase=phase))
        assert _flag_value(argv, "--phase") == phase
        assert argv.index("--phase") > argv.index("_build-job")


def test_the_verilate_jobs_envelope_and_log_are_named_for_it():
    """The verilate job's envelope and log names mirror the build job's, so a split suite's files never collide."""
    assert job_log_path("/p/artefacts/.dispatch/verilate-result-77-abc.json") == Path(
        "/p/artefacts/.dispatch/verilate-rtl_buddy-77-abc.log"
    )
    # The build job's naming is untouched.
    assert job_log_path("/p/artefacts/.dispatch/build-result-77-abc.json") == Path(
        "/p/artefacts/.dispatch/build-rtl_buddy-77-abc.log"
    )


def test_plusarg_overrides_are_forwarded_to_a_sim_job():
    """Plusarg overrides are forwarded to a sim job.

    The job must record them as this run's overrides, and a name missing from the plan falls back to re-reading tests.yaml.
    """
    argv = sim_job_argv(
        _test_spec(plusarg_overrides={"mutate": "1", "trace": None, "path": "/a=b"})
    )
    flags = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--plusarg"]
    # A valueless override is re-spelled bare, so the job's parse yields None again.
    assert flags == ["mutate=1", "trace", "path=/a=b"]
    assert argv.index("--plusarg") > argv.index("_test-job")


def test_plusarg_overrides_are_absent_when_none_were_given():
    """Plusarg overrides are absent when none were given."""
    assert "--plusarg" not in sim_job_argv(_test_spec())
    assert "--plusarg" not in sim_job_argv(_test_spec(plusarg_overrides={}))


def test_plusarg_is_not_a_build_job_flag():
    """The build job needs no plusarg overrides, since the head already merged them into the plan."""
    assert "--plusarg" not in build_job_argv(_build_spec())
