# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The ``rb`` re-entry argv a dispatched job runs.

``_rb_argv`` is the backend-independent dispatch contract, so a global the
head accepted but does not forward is silently ignored by every dispatched
job on every backend.
"""

from __future__ import annotations

from pathlib import Path

from rtl_buddy.dispatch.argv import build_job_argv, job_log_path

# Alias so pytest does not collect them as a test function and a test class.
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


def test_extra_sim_timeout_absent_when_unset():
    assert "--extra-sim-timeout" not in sim_job_argv(_test_spec())


def test_extra_sim_timeout_zero_is_forwarded_not_dropped():
    """``--extra-sim-timeout 0`` means "turn the builder's allowance off".

    A truthiness test here would drop it and leave the dispatched sim running
    with the configured allowance the caller asked to disable.
    """
    argv = sim_job_argv(_test_spec(extra_sim_timeout=0))
    assert _flag_value(argv, "--extra-sim-timeout") == "0"


def test_extra_sim_timeout_is_forwarded_to_a_build_job():
    """Build jobs never reach SIM, but the prefix is shared, so it appears."""
    argv = build_job_argv(_build_spec(extra_sim_timeout=900))
    assert _flag_value(argv, "--extra-sim-timeout") == "900"


def test_compile_parallel_is_forwarded_to_a_build_job():
    """The head's concurrency decision reaches the job through argv (#495)."""
    argv = build_job_argv(_build_spec(parallel=4))
    assert _flag_value(argv, "--parallel") == "4"
    assert argv.index("--parallel") > argv.index("_build-job")


def test_compile_parallel_is_absent_at_the_default():
    """At 1 the argv must be byte-identical to a pre-#495 head's.

    Every project that never asks for concurrency keeps today's job script,
    so a plan/manifest diff stays quiet on upgrade.
    """
    assert "--parallel" not in build_job_argv(_build_spec())
    assert "--parallel" not in build_job_argv(_build_spec(parallel=1))


def test_compile_parallel_is_not_a_sim_job_flag():
    """A sim job compiles one thing at most; the flag has no meaning there."""
    assert "--parallel" not in sim_job_argv(_test_spec())


def test_build_result_json_is_forwarded_to_a_gated_sim_job():
    """A gated job is told where the build recorded its verdict (#498).

    Without it the job cannot tell "the build's compile FAILED for me"
    from "the stamp is stale", and retries both — burning the sim
    reservation on a compile that will fail the same way, and writing its
    own failure over the build job's compile.log.
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
    """No build job, no envelope — and an argv unchanged from before #498."""
    assert "--build-result-json" not in sim_job_argv(_test_spec())
    assert "--build-result-json" not in sim_job_argv(_test_spec(expect_prebuilt=True))


def test_build_result_json_is_not_a_build_job_flag():
    """The build job WRITES the envelope; it has none to consult."""
    assert "--build-result-json" not in build_job_argv(_build_spec())


def test_builder_globals_precede_the_subcommand():
    """Globals must sit before ``_test-job`` or Typer rejects them."""
    argv = sim_job_argv(
        _test_spec(builder_override="vcs", builder_mode="reg", extra_sim_timeout=900)
    )
    sub = argv.index("_test-job")
    for flag in ("-B", "-M", "--extra-sim-timeout"):
        assert argv.index(flag) < sub, f"{flag} must precede the subcommand"


# --------------------------------------------- job_log_path (#437)


def test_job_log_path_pairs_a_sim_envelope():
    """A sim job's log sits beside its envelope, named after it."""
    assert job_log_path(
        Path("/proj/verif/blk/artefacts/alpha/dispatch/result-0003.json")
    ) == Path("/proj/verif/blk/artefacts/alpha/dispatch/rtl_buddy-0003.log")
    assert job_log_path(
        Path("/proj/verif/blk/artefacts/alpha/dispatch/result-single.json")
    ) == Path("/proj/verif/blk/artefacts/alpha/dispatch/rtl_buddy-single.log")


def test_job_log_path_pairs_a_build_envelope():
    """``build-result-<pid>`` keeps its ``build-`` prefix, so the build
    job's log does not look like a sim job's in the shared .dispatch dir."""
    assert job_log_path(
        Path("/proj/verif/blk/artefacts/.dispatch/build-result-4711.json")
    ) == Path("/proj/verif/blk/artefacts/.dispatch/build-rtl_buddy-4711.log")


def test_job_log_path_falls_back_to_the_stem():
    """A hand-written envelope name still gets a paired log, not a crash."""
    assert job_log_path(Path("/tmp/foo.json")) == Path("/tmp/rtl_buddy-foo.log")


def test_job_log_path_accepts_str_and_keeps_relative_input_relative():
    """Resolution belongs to the caller: the helper only renames."""
    assert job_log_path("dispatch/result-0001.json") == Path(
        "dispatch/rtl_buddy-0001.log"
    )
    out = job_log_path("res.json")
    assert isinstance(out, Path)
    assert out == Path("rtl_buddy-res.log")
    assert not out.is_absolute()


def test_rebuild_is_forwarded_to_a_build_job():
    """The head's ``--rebuild`` reaches the one process that may act on it
    for a whole suite: the build job (#494)."""
    argv = build_job_argv(_build_spec(rebuild=True))
    assert "--rebuild" in argv
    assert argv.index("--rebuild") > argv.index("_build-job")


def test_rebuild_is_forwarded_to_a_sim_job():
    """A suite that submitted no build job puts it on the elements instead;
    the argv contract has to carry it either way."""
    argv = sim_job_argv(_test_spec(rebuild=True))
    assert "--rebuild" in argv
    assert argv.index("--rebuild") > argv.index("_test-job")


def test_rebuild_is_absent_at_the_default():
    """Nobody asked, so the job script is the one a pre-#494 head wrote."""
    assert "--rebuild" not in build_job_argv(_build_spec())
    assert "--rebuild" not in sim_job_argv(_test_spec())
