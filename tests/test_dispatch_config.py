"""Dispatch config surface tests (#351 P1): cfg-dispatch parsing,
resources layering, and the backend registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy.config.dispatch import (
    DEFAULT_JOB_CPUS,
    DEFAULT_JOB_TIME,
    DispatchCompileFile,
    DispatchConfigFile,
    DispatchResourcesFile,
    JobResources,
    RetryConfigFile,
    combine_for_in_job_compile,
    compile_parallel,
    compile_resource_origins,
    mem_to_bytes,
    resolve_compile_resources,
    resolve_resources,
    cpu_request_overrides,
    sbatch_args_cpu_request_options,
    time_to_seconds,
)
from rtl_buddy.config.root import RootConfig
from rtl_buddy.config.suite import SuiteConfig
from rtl_buddy.dispatch import SlurmDispatchBackend, create_dispatch_backend
from rtl_buddy.errors import FatalRtlBuddyError


class _Tb:
    def __init__(self, resources=None):
        self.resources = resources


class _Test:
    def __init__(self, resources=None, tb_resources=None):
        self.resources = resources
        self._tb = _Tb(tb_resources)

    def get_testbench(self):
        return self._tb


def test_resolve_resources_builtin_defaults():
    res = resolve_resources(DispatchConfigFile())
    assert res.cpus == DEFAULT_JOB_CPUS
    assert res.mem is None
    assert res.time == DEFAULT_JOB_TIME


def test_resolve_resources_layering_most_specific_wins():
    cfg = DispatchConfigFile(
        resources=DispatchResourcesFile(cpus=2, mem="4G", time="01:00:00")
    )
    test_cfg = _Test(
        tb_resources=DispatchResourcesFile(mem="8G"),
        resources=DispatchResourcesFile(time="04:00:00"),
    )
    res = resolve_resources(cfg, test_cfg)
    assert res.cpus == 2  # from cfg-dispatch defaults
    assert res.mem == "8G"  # testbench override
    assert res.time == "04:00:00"  # test override wins over everything


def test_resolve_resources_partial_override_inherits_other_fields():
    cfg = DispatchConfigFile(
        resources=DispatchResourcesFile(cpus=4, mem="2G", time="00:30:00")
    )
    test_cfg = _Test(resources=DispatchResourcesFile(mem="24G"))
    res = resolve_resources(cfg, test_cfg)
    assert (res.cpus, res.mem, res.time) == (4, "24G", "00:30:00")


def test_create_backend_local_and_none_mean_in_process():
    cfg = DispatchConfigFile()
    assert create_dispatch_backend(None, cfg) is None
    assert create_dispatch_backend("local", cfg) is None


def test_create_backend_slurm(monkeypatch):
    import rtl_buddy.dispatch.slurm as slurm_module

    monkeypatch.setattr(slurm_module, "require_tool", lambda name: None)
    backend = create_dispatch_backend("slurm", DispatchConfigFile())
    assert isinstance(backend, SlurmDispatchBackend)


def test_create_backend_unknown_fails_loud():
    with pytest.raises(FatalRtlBuddyError, match="unknown dispatch backend"):
        create_dispatch_backend("lsf", DispatchConfigFile())


def test_root_config_parses_cfg_dispatch(minimal_project: Path):
    root_cfg_path = minimal_project / "root_config.yaml"
    root_cfg_path.write_text(
        root_cfg_path.read_text()
        + "\n"
        + "\n".join(
            [
                "cfg-dispatch:",
                "  backend: slurm",
                "  resources:",
                "    cpus: 2",
                "    mem: 4G",
                "    time: 02:00:00",
                "  sbatch-args:",
                "    - --partition=verif",
                "  poll-interval: 3",
            ]
        )
        + "\n"
    )
    root_cfg = RootConfig(name="t/root", start_dir=minimal_project)
    dispatch_cfg = root_cfg.get_dispatch_cfg()
    assert dispatch_cfg.backend == "slurm"
    assert dispatch_cfg.resources.cpus == 2
    assert dispatch_cfg.resources.mem == "4G"
    assert dispatch_cfg.resources.time == "02:00:00"
    assert dispatch_cfg.sbatch_args == ["--partition=verif"]
    assert dispatch_cfg.poll_interval == 3


def test_root_config_dispatch_defaults_when_absent(minimal_project: Path):
    root_cfg = RootConfig(name="t/root", start_dir=minimal_project)
    dispatch_cfg = root_cfg.get_dispatch_cfg()
    assert dispatch_cfg.backend is None
    assert dispatch_cfg.resources is None
    assert dispatch_cfg.sbatch_args == []


def test_tests_yaml_resources_parse_and_resolve(minimal_project: Path):
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text()
        .replace(
            "  - name: tb_basic\n",
            "  - name: tb_basic\n    resources: { cpus: 2, mem: 8G }\n",
        )
        .replace(
            "  - name: basic\n",
            "  - name: basic\n    resources: { time: 04:00:00 }\n",
        )
    )
    suite_cfg = SuiteConfig(path=str(tests_yaml))
    basic = suite_cfg.get_tests("basic")[0]
    extra = suite_cfg.get_tests("extra")[0]

    res = resolve_resources(DispatchConfigFile(), basic)
    assert (res.cpus, res.mem, res.time) == (2, "8G", "04:00:00")

    # extra has no test-level override: testbench + defaults only.
    res = resolve_resources(DispatchConfigFile(), extra)
    assert (res.cpus, res.mem, res.time) == (2, "8G", DEFAULT_JOB_TIME)


# ---------------------------------------------- P1 review: config validation


def test_poll_interval_zero_rejected():
    with pytest.raises(FatalRtlBuddyError, match="poll-interval must be > 0"):
        DispatchConfigFile(poll_interval=0.0).initialise()


def test_time_int_from_yaml_sexagesimal_rejected(minimal_project: Path):
    # `time: 4:00:00` unquoted -> YAML int 14400 -> must fail loud, not
    # silently become a 10-day reservation.
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text() + "\ncfg-dispatch:\n  resources:\n    time: 4:00:00\n"
    )
    with pytest.raises(FatalRtlBuddyError, match="parsed as an integer"):
        RootConfig(name="t/root", start_dir=minimal_project)


def test_time_quoted_ok(minimal_project: Path):
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text() + '\ncfg-dispatch:\n  resources:\n    time: "4:00:00"\n'
    )
    cfg = RootConfig(name="t/root", start_dir=minimal_project).get_dispatch_cfg()
    assert cfg.resources.time == "4:00:00"


def test_time_bad_shape_rejected():
    from rtl_buddy.config.dispatch import DispatchConfigFile, DispatchResourcesFile

    with pytest.raises(FatalRtlBuddyError, match="not a valid Slurm time"):
        DispatchConfigFile(resources=DispatchResourcesFile(time="banana")).initialise()


def test_slurm_tool_in_manifest():
    from rtl_buddy.tool_manifest import get_manifest

    slurm = next((s for s in get_manifest() if s.name == "slurm"), None)
    assert slurm is not None
    # sacct joins once right-sizing telemetry lands (P3); randtest dispatch
    # (P2) and `rb test --dispatch` (#440) add themselves to used_by.
    assert set(slurm.binaries) == {"sbatch", "squeue", "sacct", "scancel"}
    assert slurm.optional is True
    assert slurm.used_by == ("regression", "randtest", "test")


# ------------------------------------- #358: in-job compile reservations


@pytest.mark.parametrize(
    "text,expected",
    [
        ("8G", 8 * 2**30),
        ("512M", 512 * 2**20),
        ("2048K", 2048 * 2**10),
        ("1T", 2**40),
        ("4096", 4096 * 2**20),  # Slurm's default unit is MB, not bytes
        ("1.5G", int(1.5 * 2**30)),
        ("banana", None),
        (None, None),
    ],
)
def test_mem_to_bytes(text, expected):
    assert mem_to_bytes(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30", 1800),  # bare number is MINUTES
        ("05:30", 330),  # MM:SS
        ("02:00:00", 7200),  # HH:MM:SS
        ("1-00", 86400),  # DD-HH
        ("1-12:30", 86400 + 12 * 3600 + 1800),
        ("2-00:00:30", 2 * 86400 + 30),
        ("banana", None),
        (None, None),
    ],
)
def test_time_to_seconds(text, expected):
    assert time_to_seconds(text) == expected


def test_combine_takes_the_larger_of_each_field():
    """One allocation covers compile AND sim, so each field is the max."""
    sim = JobResources(cpus=1, mem="4G", time="00:30:00")
    compile_ = JobResources(cpus=8, mem="2G", time="02:00:00")
    combined, governed_by = combine_for_in_job_compile(sim, compile_)
    assert (combined.cpus, combined.mem, combined.time) == (8, "4G", "02:00:00")
    # mem stayed sim-sized because 4G > 2G; the other two came from compile.
    assert governed_by == {"cpus": "compile", "mem": "test", "time": "compile"}


def test_combine_keeps_sim_values_when_they_already_dominate():
    sim = JobResources(cpus=16, mem="64G", time="1-00:00:00")
    combined, governed_by = combine_for_in_job_compile(
        sim, JobResources(cpus=2, mem="8G", time="01:00:00")
    )
    assert (combined.cpus, combined.mem, combined.time) == (16, "64G", "1-00:00:00")
    assert set(governed_by.values()) == {"test"}


def test_combine_applies_compile_mem_when_sim_reserves_none():
    """An absent sim --mem must not swallow a compile-sized reservation."""
    combined, governed_by = combine_for_in_job_compile(
        JobResources(cpus=1, mem=None, time="01:00:00"),
        JobResources(cpus=1, mem="16G", time="01:00:00"),
    )
    assert combined.mem == "16G"
    assert governed_by["mem"] == "compile"


def test_combine_compares_across_units():
    """512M vs 1G must compare by value, not lexically."""
    combined, _ = combine_for_in_job_compile(
        JobResources(mem="512M"), JobResources(mem="1G")
    )
    assert combined.mem == "1G"
    combined, _ = combine_for_in_job_compile(
        JobResources(mem="2048M"), JobResources(mem="1G")
    )
    assert combined.mem == "2048M"


def test_combine_leaves_an_unparseable_compile_value_alone():
    """A value neither side can compare must not silently win the max."""
    combined, governed_by = combine_for_in_job_compile(
        JobResources(mem="8G", time="01:00:00"),
        JobResources(mem="lots", time="01:00:00"),
    )
    assert combined.mem == "8G"
    assert governed_by["mem"] == "test"


def test_combine_warns_when_the_compile_mem_cannot_be_parsed(caplog):
    """Silently dropping the compile mem is the one unsafe outcome."""
    import logging

    with caplog.at_level(logging.WARNING):
        combined, governed_by = combine_for_in_job_compile(
            JobResources(mem="8G"), JobResources(mem="16GB")
        )
    assert combined.mem == "8G"
    assert governed_by["mem"] == "test"
    assert "16GB" in caplog.text


def test_combine_does_not_warn_when_no_compile_mem_is_set(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        combine_for_in_job_compile(JobResources(mem="8G"), JobResources(mem=None))
    assert caplog.text == ""


# ------------------------------- #435: progress-interval / max-wait


def test_progress_and_wait_defaults_are_heartbeat_on_wait_unbounded():
    cfg = DispatchConfigFile().initialise()
    # A line a minute is cheap next to a CI console that says nothing...
    assert cfg.progress_interval == 60.0
    # ...and the deadline stays opt-in, so today's runs are unaffected.
    assert cfg.max_wait is None


def test_progress_interval_zero_is_the_quiet_terminal_not_an_error():
    assert DispatchConfigFile(progress_interval=0.0).initialise().progress_interval == 0


def test_negative_progress_interval_rejected():
    with pytest.raises(FatalRtlBuddyError, match="progress-interval must be >= 0"):
        DispatchConfigFile(progress_interval=-1.0).initialise()


def test_non_positive_max_wait_rejected():
    with pytest.raises(FatalRtlBuddyError, match="max-wait must be > 0"):
        DispatchConfigFile(max_wait=0.0).initialise()


def test_root_config_parses_progress_interval_and_max_wait(minimal_project: Path):
    root_cfg_path = minimal_project / "root_config.yaml"
    root_cfg_path.write_text(
        root_cfg_path.read_text()
        + "\ncfg-dispatch:\n  progress-interval: 30\n  max-wait: 7200\n"
    )
    cfg = RootConfig(name="t/root", start_dir=minimal_project).get_dispatch_cfg()
    assert cfg.progress_interval == 30
    assert cfg.max_wait == 7200


# ------------------------------------------------------ #405: retry budget


def test_retry_absent_is_off_and_never_retries():
    cfg = DispatchConfigFile().initialise()
    # Nothing stored at all — the block is opt-in...
    assert cfg.retry is None
    # ...and the effective budget still answers, inertly.
    retry = cfg.effective_retry()
    assert retry.attempts == 0
    assert retry.enabled is False


def test_retry_defaults_when_only_attempts_is_set():
    retry = DispatchConfigFile(retry=RetryConfigFile(attempts=2)).initialise().retry
    assert retry.enabled is True
    assert (retry.backoff_sec, retry.backoff_max_sec, retry.jitter) == (
        60.0,
        600.0,
        0.5,
    )
    assert retry.classifiers == ["license-queue"]


def test_retry_with_empty_classifier_list_retries_nothing():
    # A budget with no classifier selects nothing; treating it as "on" would
    # resubmit jobs no rule matched.
    retry = (
        DispatchConfigFile(retry=RetryConfigFile(attempts=3, classifiers=[]))
        .initialise()
        .retry
    )
    assert retry.enabled is False


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"attempts": -1}, "attempts must be >= 0"),
        ({"attempts": 1, "backoff_sec": -1.0}, "backoff-sec/backoff-max-sec"),
        (
            {"attempts": 1, "backoff_sec": 600.0, "backoff_max_sec": 60.0},
            "below backoff-sec",
        ),
        ({"attempts": 1, "jitter": 1.0}, r"jitter must be in \[0, 1\)"),
        ({"attempts": 1, "jitter": -0.1}, r"jitter must be in \[0, 1\)"),
        ({"attempts": 1, "classifiers": ["node-fail"]}, "unknown classifier"),
    ],
)
def test_retry_invalid_values_rejected(kwargs, match):
    with pytest.raises(FatalRtlBuddyError, match=match):
        DispatchConfigFile(retry=RetryConfigFile(**kwargs)).initialise()


def test_root_config_parses_retry_block(minimal_project: Path):
    root_cfg_path = minimal_project / "root_config.yaml"
    root_cfg_path.write_text(
        root_cfg_path.read_text()
        + "\n".join(
            [
                "\ncfg-dispatch:",
                "  retry:",
                "    attempts: 2",
                "    backoff-sec: 30",
                "    backoff-max-sec: 300",
                "    jitter: 0.25",
                "    classifiers: [license-queue]",
            ]
        )
        + "\n"
    )
    cfg = RootConfig(name="t/root", start_dir=minimal_project).get_dispatch_cfg()
    retry = cfg.effective_retry()
    assert retry.attempts == 2
    assert retry.backoff_sec == 30
    assert retry.backoff_max_sec == 300
    assert retry.jitter == 0.25
    assert retry.classifiers == ["license-queue"]
    assert retry.enabled is True


def test_root_config_rejects_an_unknown_classifier(minimal_project: Path):
    """The classifier list must actually bind to the YAML key.

    The happy-path test above cannot tell a parsed ``[license-queue]``
    from the default of the same value, so an inert key would look green
    there. A bogus entry can only be rejected if the key reached the
    field — which is why this list is not spelled ``on:``: PyYAML is a
    YAML 1.1 parser and an unquoted ``on`` key deserialises as the
    boolean ``True`` (#405 review).
    """
    root_cfg_path = minimal_project / "root_config.yaml"
    root_cfg_path.write_text(
        root_cfg_path.read_text() + "\ncfg-dispatch:\n  retry:\n    attempts: 2\n"
        "    classifiers: [node-fail]\n"
    )
    with pytest.raises(FatalRtlBuddyError, match="unknown classifier"):
        RootConfig(name="t/root", start_dir=minimal_project).get_dispatch_cfg()


def test_root_config_without_retry_block_leaves_it_off(minimal_project: Path):
    root_cfg_path = minimal_project / "root_config.yaml"
    root_cfg_path.write_text(
        root_cfg_path.read_text() + "\ncfg-dispatch:\n  poll-interval: 3\n"
    )
    cfg = RootConfig(name="t/root", start_dir=minimal_project).get_dispatch_cfg()
    assert cfg.retry is None
    assert cfg.effective_retry().enabled is False


# --------------------------------- #495: cfg-dispatch.compile.parallel


def test_root_config_parses_compile_parallel(minimal_project: Path):
    root_cfg_path = minimal_project / "root_config.yaml"
    root_cfg_path.write_text(
        root_cfg_path.read_text()
        + "\n"
        + "\n".join(
            [
                "cfg-dispatch:",
                "  compile:",
                "    cpus: 4",
                "    mem: 16G",
                '    time: "02:00:00"',
                "    parallel: 3",
            ]
        )
        + "\n"
    )
    cfg = RootConfig(name="t/root", start_dir=minimal_project).get_dispatch_cfg()
    assert cfg.compile.parallel == 3
    assert compile_parallel(cfg) == 3
    # The reservation fields still layer exactly as they did.
    assert resolve_compile_resources(cfg) == JobResources(
        cpus=4, mem="16G", time="02:00:00"
    )


def test_compile_parallel_defaults_to_one_with_and_without_the_block():
    absent = DispatchConfigFile().initialise()
    assert absent.compile is None
    assert compile_parallel(absent) == 1

    present = DispatchConfigFile(compile=DispatchCompileFile(cpus=8)).initialise()
    assert present.compile.parallel == 1
    assert compile_parallel(present) == 1


def test_compile_parallel_survives_the_mem_and_time_validators():
    """`parallel` rides through `_validated()`, which rebuilds the block."""
    cfg = DispatchConfigFile(
        compile=DispatchCompileFile(mem=16, time="02:00:00", parallel=4)
    ).initialise()
    assert (cfg.compile.mem, cfg.compile.time, cfg.compile.parallel) == (
        "16",
        "02:00:00",
        4,
    )


def test_compile_block_still_rejects_the_sexagesimal_time_trap():
    with pytest.raises(FatalRtlBuddyError, match="parsed as an integer"):
        DispatchConfigFile(
            compile=DispatchCompileFile(time=14400, parallel=2)
        ).initialise()


@pytest.mark.parametrize("value", [0, -1])
def test_compile_parallel_below_one_rejected(value):
    with pytest.raises(FatalRtlBuddyError, match="compile parallel must be >= 1"):
        DispatchConfigFile(compile=DispatchCompileFile(parallel=value)).initialise()


def test_parallel_never_reaches_the_resolved_compile_reservation():
    """Only the build job's own spec is scaled — never this (#495).

    The same resolved reservation sizes an in-job compile's sim job and the
    right-sizing compile floor, and both of those are one serial build.
    """
    cfg = DispatchConfigFile(
        compile=DispatchCompileFile(cpus=4, mem="16G", time="02:00:00", parallel=4)
    ).initialise()
    resolved = resolve_compile_resources(cfg)
    assert (resolved.cpus, resolved.mem, resolved.time) == (4, "16G", "02:00:00")

    combined, _ = combine_for_in_job_compile(
        JobResources(cpus=1, mem="2G", time="00:20:00"), resolved
    )
    assert combined.cpus == 4  # not 16: one in-job compile is one build


def test_parallel_is_not_a_field_of_a_cfg_dispatch_resources_block(
    minimal_project: Path,
):
    """`parallel` on `resources:` is an unknown key, not a knob (#495).

    ``resources:`` is the per-job reservation shape tests.yaml reuses at
    testbench and test level, where "compile N builds at once" means
    nothing. serde drops unknown keys, so the field simply does not exist —
    and the compile concurrency stays at its default.
    """
    root_cfg_path = minimal_project / "root_config.yaml"
    root_cfg_path.write_text(
        root_cfg_path.read_text()
        + "\ncfg-dispatch:\n  resources:\n    cpus: 2\n    parallel: 4\n"
    )
    cfg = RootConfig(name="t/root", start_dir=minimal_project).get_dispatch_cfg()
    assert cfg.resources.cpus == 2
    assert not hasattr(cfg.resources, "parallel")
    assert compile_parallel(cfg) == 1
    assert not hasattr(resolve_resources(cfg), "parallel")


def test_parallel_is_not_a_field_of_a_test_level_resources_block(
    minimal_project: Path,
):
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text().replace(
            "  - name: basic\n",
            "  - name: basic\n    resources: { cpus: 2, parallel: 4 }\n",
        )
    )
    basic = SuiteConfig(path=str(tests_yaml)).get_tests("basic")[0]
    assert basic.resources.cpus == 2
    assert not hasattr(basic.resources, "parallel")
    resolved = resolve_resources(DispatchConfigFile(), basic)
    assert resolved.cpus == 2
    assert not hasattr(resolved, "parallel")


# --- suite-level compile reservation (#497) ----------------------------


def test_suite_compile_block_is_the_most_specific_compile_layer():
    """suite `compile:` > cfg-dispatch.compile > cfg-dispatch.resources."""
    cfg = DispatchConfigFile(
        resources=DispatchResourcesFile(cpus=2, mem="4G", time="00:30:00"),
        compile=DispatchCompileFile(cpus=4, mem="16G", parallel=4),
    ).initialise()

    # No suite block: exactly the layering that shipped before.
    assert resolve_compile_resources(cfg) == JobResources(
        cpus=4, mem="16G", time="00:30:00"
    )

    # A suite block wins field by field; an omitted field still inherits.
    resolved = resolve_compile_resources(cfg, DispatchResourcesFile(mem="48G"))
    assert resolved == JobResources(cpus=4, mem="48G", time="00:30:00")


def test_suite_compile_block_layers_over_defaults_without_cfg_dispatch():
    """A project with no cfg-dispatch at all still honours the suite block."""
    resolved = resolve_compile_resources(
        None, DispatchResourcesFile(cpus=8, mem="32G", time="03:00:00")
    )
    assert resolved == JobResources(cpus=8, mem="32G", time="03:00:00")


def test_resolve_compile_resources_returns_a_fresh_object_each_call():
    """Scaling the build job's cpus must not reach the in-job compile."""
    cfg = DispatchConfigFile(compile=DispatchCompileFile(cpus=4)).initialise()
    suite_block = DispatchResourcesFile(mem="48G")
    first = resolve_compile_resources(cfg, suite_block)
    second = resolve_compile_resources(cfg, suite_block)
    assert first is not second
    first.cpus *= 4
    assert second.cpus == 4


def test_compile_resource_origins_names_only_the_fields_the_suite_set():
    assert compile_resource_origins(None) == {}
    assert compile_resource_origins(DispatchResourcesFile()) == {}
    assert compile_resource_origins(DispatchResourcesFile(mem="48G")) == {
        "mem": "suite"
    }
    assert compile_resource_origins(
        DispatchResourcesFile(cpus=8, mem="48G", time="03:00:00")
    ) == {"cpus": "suite", "mem": "suite", "time": "suite"}


def test_suite_compile_block_loads_and_validates_mem_and_time(
    minimal_project: Path,
):
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        'compile:\n  cpus: 8\n  mem: 48G\n  time: "03:00:00"\n' + tests_yaml.read_text()
    )
    suite = SuiteConfig(path=str(tests_yaml))
    block = suite.get_compile()
    assert (block.cpus, block.mem, block.time) == (8, "48G", "03:00:00")
    # mem is normalised to a string by the same validator cfg-dispatch uses.
    assert isinstance(block.mem, str)


def test_suite_compile_block_absent_resolves_to_none(minimal_project: Path):
    suite = SuiteConfig(path=str(minimal_project / "tests.yaml"))
    assert suite.get_compile() is None
    assert (
        resolve_compile_resources(
            DispatchConfigFile().initialise(), suite.get_compile()
        )
        == JobResources()
    )


def test_suite_compile_block_rejects_the_sexagesimal_time_trap(
    minimal_project: Path,
):
    """An unquoted `3:00:00` is the integer 10800 by the time serde sees it."""
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text("compile:\n  time: 3:00:00\n" + tests_yaml.read_text())
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        SuiteConfig(path=str(tests_yaml))
    assert "sexagesimal" in str(excinfo.value)


def test_suite_compile_block_rejects_a_malformed_time(minimal_project: Path):
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text('compile:\n  time: "3 hours"\n' + tests_yaml.read_text())
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        SuiteConfig(path=str(tests_yaml))
    assert "not a valid Slurm time" in str(excinfo.value)


def test_suite_compile_block_coerces_an_integer_mem(minimal_project: Path):
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text("compile:\n  mem: 4096\n" + tests_yaml.read_text())
    assert SuiteConfig(path=str(tests_yaml)).get_compile().mem == "4096"


def test_parallel_is_not_a_field_of_a_suite_level_compile_block(
    minimal_project: Path,
):
    """`parallel` at suite level is an unknown key, not a knob (#497).

    Concurrency sizes the build job against the partition's widest node,
    which is a cluster fact; serde drops the key, so the resolved
    reservation is untouched and cfg-dispatch still owns the concurrency.
    """
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        "compile:\n  mem: 48G\n  parallel: 4\n" + tests_yaml.read_text()
    )
    block = SuiteConfig(path=str(tests_yaml)).get_compile()
    assert block.mem == "48G"
    assert not hasattr(block, "parallel")

    cfg = DispatchConfigFile(compile=DispatchCompileFile(cpus=4)).initialise()
    assert compile_parallel(cfg) == 1
    resolved = resolve_compile_resources(cfg, block)
    assert resolved == JobResources(cpus=4, mem="48G")
    assert not hasattr(resolved, "parallel")


# ------------- #505 review: a cpus override in sbatch-args is detectable


@pytest.mark.parametrize(
    "args,expected",
    [
        (["--cpus-per-task=4"], "--cpus-per-task=4"),
        (["--cpus-per-task", "4"], "--cpus-per-task 4"),
        (["-c", "4"], "-c 4"),
        (["-c4"], "-c4"),
        (["-c=4"], "-c=4"),
        (
            ["--partition=verif", "--cpus-per-task=4", "--exclusive"],
            "--cpus-per-task=4",
        ),
        # A trailing flag with no value is malformed sbatch input, but it is
        # still an override of intent: sbatch, not right-sizing, reports it.
        (["--cpus-per-task"], "--cpus-per-task"),
        # `ReqCPUS` is tasks x cpus-per-task, so a task count multiplies the
        # job's cpus just as surely and leaves `requested_cpus` — a per-task
        # number — under-counting the denominator (#505 review).
        (["--ntasks=4"], "--ntasks=4"),
        (["-n", "4"], "-n 4"),
        (["-n4"], "-n4"),
        # sbatch documents this one as a REQUEST when `--ntasks` is absent
        # ("meant to be used with the --nodes option"), so `--nodes=2
        # --ntasks-per-node=4` asks for eight tasks (#505 review).
        (["--ntasks-per-node=2"], "--ntasks-per-node=2"),
        (["--nodes=2"], "--nodes=2"),
        (["-N", "2"], "-N 2"),
    ],
)
def test_a_cpus_override_in_sbatch_args_is_found(args, expected):
    """`sbatch-args` is appended last and wins, so it has to be seen (#505).

    Right-sizing otherwise analyses a run against the cpus the YAML resolved
    to rather than the cpus it was submitted with — the same class of bug
    #505 fixed, arriving by the other door.
    """
    assert sbatch_args_cpu_request_options(args) == [expected]


@pytest.mark.parametrize(
    "args,expected",
    [
        (["-c", "4", "--cpus-per-task=8"], "--cpus-per-task=8"),
        (["--cpus-per-task=8", "-c", "4"], "-c 4"),
        (["-c4", "--partition=verif", "-c8"], "-c8"),
        (["-n", "2", "--ntasks=8"], "--ntasks=8"),
    ],
)
def test_the_last_occurrence_of_ONE_option_wins_like_sbatch(args, expected):
    """sbatch obeys the FINAL occurrence, so the hint must name that one.

    `[-c, 4, --cpus-per-task=8]` runs with 8; naming the first would send a
    reader to an argument that is not in force. The short and long spellings
    are the SAME option, so this is one entry and not two (#505 review).
    """
    assert sbatch_args_cpu_request_options(args) == [expected]


@pytest.mark.parametrize(
    "args,expected",
    [
        (["--ntasks=4", "--cpus-per-task=2"], ["--ntasks=4", "--cpus-per-task=2"]),
        # Order of first appearance, so the note reads back in the order the
        # project wrote them.
        (["--cpus-per-task=2", "--ntasks=4"], ["--cpus-per-task=2", "--ntasks=4"]),
        (["-n", "4", "-c", "2"], ["-n 4", "-c 2"]),
        # Repetition still collapses per option, and keeps that option's
        # first position while carrying its last value.
        (
            ["-n", "4", "--cpus-per-task=2", "--ntasks=8"],
            ["--ntasks=8", "--cpus-per-task=2"],
        ),
        (
            ["--nodes=2", "--ntasks-per-node=4", "--cpus-per-task=2"],
            ["--nodes=2", "--ntasks-per-node=4", "--cpus-per-task=2"],
        ),
        # An excluded neighbour alongside a real one changes nothing.
        (
            ["--threads-per-core=2", "--ntasks=4", "--cpus-per-task=2"],
            ["--ntasks=4", "--cpus-per-task=2"],
        ),
    ],
)
def test_distinct_cpu_options_multiply_instead_of_overriding(args, expected):
    """`--ntasks` and `--cpus-per-task` are orthogonal, not rivals.

    `ReqCPUS` is their product, so "the last one wins" is simply wrong
    across different options: neither is superseded, and neither alone can
    be handed a whole-job suggestion (#505 review).
    """
    assert sbatch_args_cpu_request_options(args) == expected


@pytest.mark.parametrize(
    "args",
    [
        [],
        None,
        ["--partition=verif"],
        ["--constraint=haswell"],
        ["--comment=nightly"],
        ["--chdir=/tmp"],
        ["--mem=8G", "--time=01:00:00"],
        # Not a cpus count: the short options take a number.
        ["-cfoo"],
        ["-nodes"],
        # `--nodelist` shares a prefix with `--nodes`, and `--ntasks-per-*`
        # must not be read as a bare `--ntasks`.
        ["--nodelist=node01"],
        # Allocation, not request: `ReqCPUS` still describes the reservation,
        # so the fallback is already right and there is nothing to retarget.
        ["--exclusive"],
        ["--overcommit"],
        # Node SELECTION, not request: these restrict which nodes and
        # hardware threads may be used while the generated `--cpus-per-task`
        # still states the request, so the head still knows it and must not
        # throw it away (#505 review).
        ["--threads-per-core=2"],
        ["--threads-per-core", "2"],
        ["-B", "2:4:1"],
        ["--extra-node-info=2:4:1"],
        # Mutually exclusive with the `--cpus-per-task` every dispatched job
        # carries, so sbatch rejects the pair: the "override" can never take
        # effect, and a job that never runs has nothing to right-size.
        ["--cpus-per-gpu=4"],
        ["--cpus-per-gpu", "4"],
        # Placement MAXIMA, not requests: they cap where the tasks
        # `--ntasks` asked for may land ("request the maximum ntasks be
        # invoked on each core/socket ... meant to be used with the
        # --ntasks option"), so a lone one requests nothing. The `--ntasks`
        # they accompany is in the set, so a real change is still caught.
        ["--ntasks-per-core=2"],
        ["--ntasks-per-socket=2"],
        # Only takes effect beside a GPU request rtl-buddy neither generates
        # nor tracks, so on its own it moves no cpu request.
        ["--ntasks-per-gpu=2"],
    ],
)
def test_args_that_do_not_touch_cpus_are_left_alone(args):
    """`--constraint`/`--comment`/`--chdir` all start with `--c`."""
    assert sbatch_args_cpu_request_options(args) == []


def test_the_override_is_reported_verbatim_for_the_log_line():
    assert sbatch_args_cpu_request_options(["--cpus-per-task=4"]) == [
        "--cpus-per-task=4"
    ]
    assert sbatch_args_cpu_request_options(["-c", "4"]) == ["-c 4"]
    assert sbatch_args_cpu_request_options(["--x", "--cpus-per-task", "16"]) == [
        "--cpus-per-task 16"
    ]


# ---- #505 review: the SBATCH_* environment overrides the request too


def test_an_sbatch_env_var_is_an_override_on_its_own():
    """`subprocess.run` inherits the environment, so sbatch reads it.

    `SBATCH_NTASKS=4` beside a generated `--cpus-per-task=2` requests eight
    cpus, while the head recorded two — efficiency would be overstated
    fourfold. The variable is not sanitized away (a site that exports it
    means it); it is recognised (#505 review).
    """
    assert cpu_request_overrides([], {"SBATCH_NTASKS": "4"}) == ["SBATCH_NTASKS=4"]
    assert cpu_request_overrides([], {"SBATCH_NODES": "2"}) == ["SBATCH_NODES=2"]
    assert cpu_request_overrides([], {"SBATCH_NTASKS_PER_NODE": "2"}) == [
        "SBATCH_NTASKS_PER_NODE=2"
    ]


def test_env_and_sbatch_args_are_both_reported():
    """Different options, so they combine rather than supersede."""
    assert cpu_request_overrides(["--cpus-per-task=2"], {"SBATCH_NTASKS": "4"}) == [
        "--cpus-per-task=2",
        "SBATCH_NTASKS=4",
    ]


def test_an_explicit_sbatch_arg_beats_the_environment():
    """sbatch's own precedence: command line > environment > script.

    The job runs with `--ntasks=8`, so naming the variable would send a
    reader to a setting that is not in force — the same mistake as naming
    the first of two occurrences of one option.
    """
    assert cpu_request_overrides(["--ntasks=8"], {"SBATCH_NTASKS": "4"}) == [
        "--ntasks=8"
    ]
    # ...and only for the SAME option: an unrelated argument does not shadow
    # the variable.
    assert cpu_request_overrides(["--cpus-per-task=8"], {"SBATCH_NTASKS": "4"}) == [
        "--cpus-per-task=8",
        "SBATCH_NTASKS=4",
    ]


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_env_var_is_not_an_override(value):
    """Exported-but-empty is how a shell unsets one in practice."""
    assert cpu_request_overrides([], {"SBATCH_NTASKS": value}) == []


def test_an_absent_environment_changes_nothing():
    assert cpu_request_overrides([], {}) == []
    assert cpu_request_overrides(["--ntasks=4"], {}) == ["--ntasks=4"]


def test_sbatch_cpus_per_task_env_is_not_an_override():
    """The generated `--cpus-per-task` always beats it.

    sbatch's precedence is command line > environment, and both submit
    paths emit `--cpus-per-task` unconditionally, so the variable can never
    take effect. Treating it as an override would discard a request the
    head knows — the same false positive `--cpus-per-gpu` was excluded for
    (#505 review). `test_dispatch_slurm.py` pins the flag's presence, which
    is what makes this true.
    """
    assert cpu_request_overrides([], {"SBATCH_CPUS_PER_TASK": "4"}) == []
    # The variables Slurm defines for the options this set already excludes
    # are out for their own reasons, and stay out.
    assert cpu_request_overrides([], {"SBATCH_THREADS_PER_CORE": "2"}) == []
    assert cpu_request_overrides([], {"SBATCH_CPUS_PER_GPU": "4"}) == []
    assert cpu_request_overrides([], {"SBATCH_EXCLUSIVE": "1"}) == []


def test_the_env_layer_reads_os_environ_by_default(monkeypatch):
    """No `env=` argument means the environment the jobs will inherit."""
    monkeypatch.delenv("SBATCH_NTASKS", raising=False)
    assert cpu_request_overrides([]) == []
    monkeypatch.setenv("SBATCH_NTASKS", "4")
    assert cpu_request_overrides([]) == ["SBATCH_NTASKS=4"]


def test_the_args_only_scanner_ignores_the_environment(monkeypatch):
    """`sbatch_args_cpu_request_options` stays what its name says."""
    monkeypatch.setenv("SBATCH_NTASKS", "4")
    assert sbatch_args_cpu_request_options([]) == []
