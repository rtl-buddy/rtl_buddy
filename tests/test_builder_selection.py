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


# --- summary footer: builder reported as a list -----------------------------


class _NamedBuilder:
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


class _FakeTest:
    def __init__(self, builder_name):
        self._builder_name = builder_name

    def get_builder_name(self):
        return self._builder_name


class _FakeSuite:
    def __init__(self, builder_names):
        self._tests = [_FakeTest(b) for b in builder_names]

    def get_tests(self, _test_name=None):
        return self._tests


def _make_rtl_buddy(builders, builder_override=None, platform="verilator"):
    """Build an RtlBuddy with just enough wiring for _builder_metadata_line."""
    from rtl_buddy.rtl_buddy import RtlBuddy

    rb = RtlBuddy.__new__(RtlBuddy)
    rb.root_cfg = _make_root(
        builders[platform], builders, builder_override=builder_override
    )
    rb.builder = platform
    return rb


def test_builder_footer_single_builder():
    builders = {
        "verilator": _NamedBuilder("verilator"),
        "icarus": _NamedBuilder("icarus"),
    }
    rb = _make_rtl_buddy(builders)
    suite = _FakeSuite(["icarus", "icarus"])
    assert rb._builder_metadata_line(suite) == "Builder: icarus"


def test_builder_footer_lists_multiple_builders_sorted():
    builders = {
        "verilator": _NamedBuilder("verilator"),
        "icarus": _NamedBuilder("icarus"),
    }
    rb = _make_rtl_buddy(builders)
    # Mixed suite: one test pins icarus, one falls back to platform (verilator).
    suite = _FakeSuite(["icarus", None])
    assert rb._builder_metadata_line(suite) == "Builders: icarus, verilator"


def test_builder_footer_cli_override_collapses_to_one():
    builders = {
        "verilator": _NamedBuilder("verilator"),
        "icarus": _NamedBuilder("icarus"),
    }
    rb = _make_rtl_buddy(builders, builder_override="verilator")
    # Override forces every test onto verilator, so the footer lists only it.
    suite = _FakeSuite(["icarus", None])
    assert rb._builder_metadata_line(suite) == "Builder: verilator"


def test_builder_footer_unions_across_regression_suites():
    builders = {
        "verilator": _NamedBuilder("verilator"),
        "icarus": _NamedBuilder("icarus"),
    }
    rb = _make_rtl_buddy(builders)
    suites = [_FakeSuite(["verilator"]), _FakeSuite(["icarus"])]
    assert rb._builder_metadata_line(suites) == "Builders: icarus, verilator"


# --- summary table: per-row Builder column when >1 builder ------------------


class _FakeResults:
    def __init__(self, result="PASS", desc="d"):
        self.results = {"result": result, "desc": desc}


class _FakeCoverage:
    def format_summary(self, _results):
        return None


def _result_row(test_name, builder):
    return {
        "test_name": test_name,
        "randmode_i": None,
        "results": _FakeResults(),
        "builder": builder,
    }


def _capture_test_summary(monkeypatch, suite_results):
    import rtl_buddy.rtl_buddy as rbmod

    rb = _make_rtl_buddy({"verilator": _NamedBuilder("verilator")})
    rb.coverage = _FakeCoverage()
    captured = {}
    monkeypatch.setattr(rbmod, "render_summary", lambda **kw: captured.update(kw))
    rb._render_test_summary("Test Results Summary", suite_results)
    return captured


def test_summary_omits_builder_column_for_single_builder(monkeypatch):
    captured = _capture_test_summary(
        monkeypatch, [_result_row("a", "icarus"), _result_row("b", "icarus")]
    )
    assert "builder" not in [key for key, _ in captured["columns"]]


def test_summary_adds_builder_column_when_multiple_builders(monkeypatch):
    captured = _capture_test_summary(
        monkeypatch, [_result_row("a", "icarus"), _result_row("b", "verilator")]
    )
    assert ("builder", "Builder") in captured["columns"]
    assert {row["builder"] for row in captured["rows"]} == {"icarus", "verilator"}
