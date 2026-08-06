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

from rtl_buddy.dispatch.argv import build_job_argv

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


def test_builder_globals_precede_the_subcommand():
    """Globals must sit before ``_test-job`` or Typer rejects them."""
    argv = sim_job_argv(
        _test_spec(builder_override="vcs", builder_mode="reg", extra_sim_timeout=900)
    )
    sub = argv.index("_test-job")
    for flag in ("-B", "-M", "--extra-sim-timeout"):
        assert argv.index(flag) < sub, f"{flag} must precede the subcommand"
