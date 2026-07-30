"""Dispatch config surface tests (#351 P1): cfg-dispatch parsing,
resources layering, and the backend registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy.config.dispatch import (
    DEFAULT_JOB_CPUS,
    DEFAULT_JOB_TIME,
    DispatchConfigFile,
    DispatchResourcesFile,
    resolve_resources,
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
    # (P2) adds it to used_by.
    assert set(slurm.binaries) == {"sbatch", "squeue", "sacct", "scancel"}
    assert slurm.optional is True
    assert slurm.used_by == ("regression", "randtest")
