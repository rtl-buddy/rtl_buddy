# rtl-buddy
# vim: set sw=2:ts=2:et:
"""Tests for `filelist-modes`: extra compile inputs for one builder mode, at suite or testbench level."""

from textwrap import dedent

import pytest

from rtl_buddy.config import test as test_config
from rtl_buddy.config.rtl import normalize_filelist_modes
from rtl_buddy.config.suite import SuiteConfig
from rtl_buddy.errors import FatalRtlBuddyError


class _FakeModelLoader:
    def __init__(self, *_args, **_kwargs):
        pass

    def get_model(self, _name):
        return object()


@pytest.fixture
def _patch_model_loader(monkeypatch):
    monkeypatch.setattr("rtl_buddy.config.test.ModelConfigLoader", _FakeModelLoader)


def _write_suite(tmp_path, suite_extra="", tb_extra=""):
    path = tmp_path / "tests.yaml"
    path.write_text(
        dedent(
            """
            rtl-buddy-filetype: test_config
            {suite_extra}
            testbenches:
              - name: tb_a
                filelist: [tb_a.sv]
                {tb_extra}
              - name: tb_b
                filelist: [tb_b.sv]
            tests:
              - name: one
                desc: ""
                model: m
                model_path: models.yaml
                reglvl: 0
                testbench: tb_a
              - name: two
                desc: ""
                model: m
                model_path: models.yaml
                reglvl: 0
                testbench: tb_b
            """
        ).format(suite_extra=suite_extra, tb_extra=tb_extra)
    )
    return str(path)


def test_entries_normalize_per_mode():
    assert normalize_filelist_modes(
        {"cov": ["a_scope.vlt", "+define+COV"], "reg": None, "default": []},
        where="s",
    ) == {"cov": ["a_scope.vlt", "+define+COV"], "reg": [], "default": []}
    assert normalize_filelist_modes(None, where="s") is None


@pytest.mark.parametrize(
    "value, message",
    [
        (["a.vlt"], "must map a builder mode"),
        ("a.vlt", "must map a builder mode"),
        ({"": ["a.vlt"]}, "keys must be builder mode names"),
        ({3: ["a.vlt"]}, "keys must be builder mode names"),
        ({"cov": "a.vlt"}, r"filelist-modes\.cov must be a list"),
        ({"cov": ["a.vlt", 1]}, r"filelist-modes\.cov must be a list"),
    ],
)
def test_malformed_values_name_where(value, message):
    with pytest.raises(FatalRtlBuddyError, match=message) as err:
        normalize_filelist_modes(value, where="verif/blk/tests.yaml")
    assert "verif/blk/tests.yaml" in str(err.value)


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("cov", ["a_scope.vlt"]),  # the mode's own entry
        ("reg", ["base.vlt"]),  # no entry: `default`
        (None, ["base.vlt"]),  # no mode known: `default`
        ("nightly", []),  # an empty entry selects nothing, not `default`
    ],
)
def test_a_testbench_selects_the_mode_then_default(mode, expected):
    tb = test_config.TestbenchConfig(
        name="tb",
        filelist=["tb.sv"],
        filelist_modes={
            "cov": ["a_scope.vlt"],
            "default": ["base.vlt"],
            "nightly": [],
        },
    )
    assert tb.get_mode_filelist(mode) == expected
    # The every-mode filelist is untouched.
    assert tb.get_filelist() == ["tb.sv"]


def test_a_testbench_without_the_key_adds_nothing():
    tb = test_config.TestbenchConfig(name="tb", filelist=["tb.sv"])
    assert tb.filelist_modes is None
    assert tb.get_mode_filelist("cov") == []


def test_a_malformed_testbench_entry_names_the_testbench():
    with pytest.raises(FatalRtlBuddyError, match="testbench 'tb'.*filelist-modes"):
        test_config.TestbenchConfig(
            name="tb", filelist=[], filelist_modes={"cov": "a.vlt"}
        )


def test_a_testbench_entry_applies_to_that_testbench_only(
    tmp_path, _patch_model_loader
):
    suite = SuiteConfig(
        _write_suite(tmp_path, tb_extra="filelist-modes: {cov: [a_scope.vlt]}")
    )
    assert suite.tests["one"].get_mode_filelist("cov") == ["a_scope.vlt"]
    assert suite.tests["one"].get_mode_filelist("reg") == []
    assert suite.tests["two"].get_mode_filelist("cov") == []


def test_suite_entries_apply_to_every_testbench_before_its_own(
    tmp_path, _patch_model_loader
):
    suite = SuiteConfig(
        _write_suite(
            tmp_path,
            suite_extra="filelist-modes: {cov: [suite.vlt], default: [all.vlt]}",
            tb_extra="filelist-modes: {cov: [a_scope.vlt]}",
        )
    )
    assert suite.tests["one"].get_mode_filelist("cov") == ["suite.vlt", "a_scope.vlt"]
    assert suite.tests["two"].get_mode_filelist("cov") == ["suite.vlt"]
    # Each level falls back to its own `default` independently.
    assert suite.tests["one"].get_mode_filelist("reg") == ["all.vlt"]


def test_a_malformed_suite_key_fails_the_load(tmp_path, _patch_model_loader):
    with pytest.raises(FatalRtlBuddyError, match="filelist-modes.cov must be a list"):
        SuiteConfig(_write_suite(tmp_path, suite_extra="filelist-modes: {cov: 7}"))


def test_a_malformed_testbench_key_fails_the_load(tmp_path, _patch_model_loader):
    with pytest.raises(FatalRtlBuddyError) as err:
        SuiteConfig(_write_suite(tmp_path, tb_extra="filelist-modes: [a.vlt]"))
    chain = [err.value]
    while chain[-1].__cause__ is not None:
        chain.append(chain[-1].__cause__)
    assert any("filelist-modes" in str(e) for e in chain)


def test_the_dispatch_plan_round_trip_keeps_both_levels(
    tmp_path, monkeypatch, _patch_model_loader
):
    suite = SuiteConfig(
        _write_suite(
            tmp_path,
            suite_extra="filelist-modes: {cov: [suite.vlt]}",
            tb_extra="filelist-modes: {cov: [a_scope.vlt], default: [base.vlt]}",
        )
    )
    monkeypatch.setattr(test_config, "ModelConfigLoader", _FakeModelLoader)
    plan = suite.tests["one"].to_plan_dict()
    plan["model"] = None
    monkeypatch.setattr(
        test_config,
        "from_dict",
        lambda cls, obj: (
            None if cls is test_config.ModelConfig else _from_dict(cls, obj)
        ),
    )
    rebuilt = test_config.TestConfig.from_plan_dict(plan)
    assert rebuilt.get_mode_filelist("cov") == ["suite.vlt", "a_scope.vlt"]
    assert rebuilt.get_mode_filelist("reg") == ["base.vlt"]
    # A plan written before the field existed still loads, with no suite entries.
    del plan["filelist_modes"]
    assert test_config.TestConfig.from_plan_dict(plan).get_mode_filelist("cov") == [
        "a_scope.vlt"
    ]


_from_dict = test_config.from_dict
