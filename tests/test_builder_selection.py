# rtl-buddy
# vim: set sw=2:ts=2:et:
"""Tests for per-suite / per-test simulator builder selection (`builder:`)."""

from textwrap import dedent

import pytest
from serde.yaml import from_yaml

from rtl_buddy.config.root import RootConfig
from rtl_buddy.config.suite import SuiteConfigFile
from rtl_buddy.config.test import TestConfigFile
from rtl_buddy.errors import FatalRtlBuddyError


# --- YAML schema parsing -----------------------------------------------------


def test_suite_config_parses_suite_and_per_test_builder():
    yaml = dedent(
        """
        rtl-buddy-filetype: test_config
        builder: icarus
        testbenches:
          - name: tb
            filelist: [tb.sv]
        tests:
          - name: inherits_suite
            desc: ""
            model: m
            model_path: models.yaml
            reglvl: 0
            plusargs: null
            plusdefines: null
            uvm: null
            testbench: tb
            sim_timeout: null
          - name: explicit
            desc: ""
            model: m
            model_path: models.yaml
            reglvl: 0
            plusargs: null
            plusdefines: null
            uvm: null
            testbench: tb
            sim_timeout: null
            builder: verilator
        """
    )
    cfg = from_yaml(SuiteConfigFile, yaml)
    assert cfg.builder == "icarus"
    by_name = {t.name: t for t in cfg.tests}
    # The suite-wide default is applied at initialise(), not on the file object.
    assert by_name["inherits_suite"].builder_name is None
    assert by_name["explicit"].builder_name == "verilator"


def test_suite_config_builder_defaults_to_none_when_absent():
    yaml = dedent(
        """
        rtl-buddy-filetype: test_config
        testbenches:
          - name: tb
            filelist: [tb.sv]
        tests: []
        """
    )
    cfg = from_yaml(SuiteConfigFile, yaml)
    assert cfg.builder is None


# --- initialise() suite/per-test fallback ------------------------------------


class _FakeModelLoader:
    def __init__(self, *_args, **_kwargs):
        pass

    def get_model(self, _name):
        return object()


def _make_test_file(name, builder=None):
    return TestConfigFile(
        name=name,
        desc="",
        model="m",
        model_path="models.yaml",
        _reglvl=0,
        pa=None,
        pd=None,
        uvm=None,
        preproc_path=None,
        postproc_path=None,
        sweep_path=None,
        tb="tb",
        timeout=None,
        builder_name=builder,
    )


@pytest.fixture
def _patch_model_loader(monkeypatch):
    monkeypatch.setattr("rtl_buddy.config.test.ModelConfigLoader", _FakeModelLoader)


def test_initialise_per_test_builder_wins_over_suite(_patch_model_loader):
    tbs = {"tb": object()}
    tc = _make_test_file("t", builder="icarus").initialise(
        ".", tbs, suite_builder="verilator"
    )
    assert tc.get_builder_name() == "icarus"


def test_initialise_falls_back_to_suite_builder(_patch_model_loader):
    tbs = {"tb": object()}
    tc = _make_test_file("t", builder=None).initialise(
        ".", tbs, suite_builder="verilator"
    )
    assert tc.get_builder_name() == "verilator"


def test_initialise_no_builder_anywhere_is_none(_patch_model_loader):
    tbs = {"tb": object()}
    tc = _make_test_file("t").initialise(".", tbs)
    assert tc.get_builder_name() is None


# --- RootConfig.resolve_rtl_builder_cfg precedence ---------------------------


class _FakePlatform:
    def __init__(self, builder):
        self._builder = builder

    def get_builder(self):
        return self._builder


def _make_root(platform_builder, builders, builder_override=None):
    """Build a RootConfig without touching disk/platform detection."""
    root = RootConfig.__new__(RootConfig)
    root.rtl_builder_cfgs = builders
    root.builder_override = builder_override
    root.platform_cfg = _FakePlatform(platform_builder)
    return root


def test_resolve_returns_platform_default_when_no_builder_requested():
    verilator, icarus = object(), object()
    root = _make_root(verilator, {"verilator": verilator, "icarus": icarus})
    assert root.resolve_rtl_builder_cfg(None) is verilator


def test_resolve_uses_per_test_builder_when_requested():
    verilator, icarus = object(), object()
    root = _make_root(verilator, {"verilator": verilator, "icarus": icarus})
    assert root.resolve_rtl_builder_cfg("icarus") is icarus


def test_cli_builder_override_forces_builder_over_per_test():
    verilator, icarus = object(), object()
    # builder_override means platform_cfg already resolves to the forced builder.
    root = _make_root(
        verilator,
        {"verilator": verilator, "icarus": icarus},
        builder_override="verilator",
    )
    assert root.resolve_rtl_builder_cfg("icarus") is verilator


def test_resolve_unknown_builder_name_raises():
    verilator = object()
    root = _make_root(verilator, {"verilator": verilator})
    with pytest.raises(FatalRtlBuddyError):
        root.resolve_rtl_builder_cfg("nonexistent")
