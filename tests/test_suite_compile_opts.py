# rtl-buddy
# vim: set sw=2:ts=2:et:
"""Tests for a suite's `compile-opts`: options per simulator family appended to the builder mode's compile-time."""

from textwrap import dedent

import pytest

from rtl_buddy.config.rtl import normalize_compile_opts
from rtl_buddy.config.suite import SuiteConfig
from rtl_buddy.config import test as test_config
from rtl_buddy.errors import FatalRtlBuddyError


class _FakeModelLoader:
    def __init__(self, *_args, **_kwargs):
        pass

    def get_model(self, _name):
        return object()


@pytest.fixture
def _patch_model_loader(monkeypatch):
    monkeypatch.setattr("rtl_buddy.config.test.ModelConfigLoader", _FakeModelLoader)


def _write_suite(tmp_path, extra=""):
    path = tmp_path / "tests.yaml"
    path.write_text(
        dedent(
            """
            rtl-buddy-filetype: test_config
            {extra}
            testbenches:
              - name: tb
                filelist: [tb.sv]
            tests:
              - name: one
                desc: ""
                model: m
                model_path: models.yaml
                reglvl: 0
                testbench: tb
              - name: two
                desc: ""
                model: m
                model_path: models.yaml
                reglvl: 0
                testbench: tb
            """
        ).format(extra=extra)
    )
    return str(path)


def test_a_string_splits_on_whitespace_like_compile_time():
    assert normalize_compile_opts(
        {"verilator": "  -MAKEFLAGS   OPT_FAST=-O0\n -CFLAGS -g "}, where="s"
    ) == {"verilator": ["-MAKEFLAGS", "OPT_FAST=-O0", "-CFLAGS", "-g"]}


def test_a_list_is_taken_token_by_token():
    assert normalize_compile_opts({"vcs": ["-kdb", "+lint=TFIPC-L"]}, where="s") == {
        "vcs": ["-kdb", "+lint=TFIPC-L"]
    }


def test_family_names_are_matched_case_insensitively():
    assert normalize_compile_opts({"Verilator": "-O3"}, where="s") == {
        "verilator": ["-O3"]
    }


def test_absent_and_empty_values():
    assert normalize_compile_opts(None, where="s") is None
    assert normalize_compile_opts({"verilator": None, "vcs": ""}, where="s") == {
        "verilator": [],
        "vcs": [],
    }


@pytest.mark.parametrize(
    "value, message",
    [
        ("-O0", "must map a simulator family"),
        (["-O0"], "must map a simulator family"),
        ({"": "-O0"}, "keys must be simulator family names"),
        ({"verilator": 3}, "must be a string or a list of strings"),
        ({"verilator": ["-O0", 1]}, "must be a string or a list of strings"),
    ],
)
def test_malformed_values_name_the_suite(value, message):
    with pytest.raises(FatalRtlBuddyError, match=message) as err:
        normalize_compile_opts(value, where="verif/blk/tests.yaml")
    assert "verif/blk/tests.yaml" in str(err.value)


def test_every_test_in_the_suite_carries_the_options(tmp_path, _patch_model_loader):
    suite = SuiteConfig(
        _write_suite(
            tmp_path,
            'compile-opts:\n  verilator: "-MAKEFLAGS OPT_FAST=-O0"\n  vcs: [-kdb]',
        )
    )
    for test in suite.tests.values():
        assert test.get_compile_opts("verilator") == ["-MAKEFLAGS", "OPT_FAST=-O0"]
        assert test.get_compile_opts("vcs") == ["-kdb"]
        assert test.get_compile_opts("icarus") == []


def test_a_suite_without_the_key_adds_nothing(tmp_path, _patch_model_loader):
    suite = SuiteConfig(_write_suite(tmp_path))
    assert all(t.get_compile_opts("verilator") == [] for t in suite.tests.values())


def test_a_malformed_key_fails_the_suite_load(tmp_path, _patch_model_loader):
    with pytest.raises(FatalRtlBuddyError, match="must be a string or a list"):
        SuiteConfig(_write_suite(tmp_path, "compile-opts:\n  verilator: 7"))


def test_the_dispatch_plan_round_trip_keeps_the_options(tmp_path, monkeypatch):
    monkeypatch.setattr(test_config, "to_dict", lambda obj: obj)
    monkeypatch.setattr(test_config, "from_dict", lambda _cls, obj: obj)
    monkeypatch.setattr(test_config, "ModelConfigLoader", _FakeModelLoader)
    suite = SuiteConfig(
        _write_suite(tmp_path, 'compile-opts:\n  verilator: "-MAKEFLAGS OPT_FAST=-O0"')
    )
    plan = suite.tests["one"].to_plan_dict()
    assert plan["compile_opts"] == {"verilator": ["-MAKEFLAGS", "OPT_FAST=-O0"]}
    rebuilt = test_config.TestConfig.from_plan_dict(plan)
    assert rebuilt.get_compile_opts("verilator") == ["-MAKEFLAGS", "OPT_FAST=-O0"]
    # A plan written before the field existed still loads, with no options.
    del plan["compile_opts"]
    assert (
        test_config.TestConfig.from_plan_dict(plan).get_compile_opts("verilator") == []
    )
