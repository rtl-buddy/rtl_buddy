"""A config entry that leaves out a required key fails to load naming the entry and the key (`yaml_loader.missing_keys`), in every suite family, instead of a bare `KeyError`."""

from textwrap import dedent

import pytest
from serde import field, serde

from rtl_buddy.config.cdc import CdcSuiteConfig
from rtl_buddy.config.fpga import FpgaSuiteConfig
from rtl_buddy.config.fpv import FpvSuiteConfig
from rtl_buddy.config.lint import LintSuiteConfig
from rtl_buddy.config.pnr import PnrSuiteConfig
from rtl_buddy.config.power import PowerSuiteConfig
from rtl_buddy.config.suite import SuiteConfig
from rtl_buddy.config.synth import SynthSuiteConfig
from rtl_buddy.config.yaml_loader import MissingKeyError, config_from_data, missing_keys
from rtl_buddy.errors import FatalRtlBuddyError


@serde
class _Leaf:
    need: str
    maybe: str | None
    has: int = 0


@serde
class _Entry:
    name: str
    long_name: str = field(rename="long-name")
    leaf: _Leaf | None = None
    leaves: list[_Leaf] = field(default_factory=list)
    by_key: dict[str, _Leaf] = field(rename="by-key", default_factory=dict)
    raw: _Leaf | None = field(default=None, deserializer=lambda data: data)


@serde
class _File:
    runs: list[_Entry]


def test_missing_keys_names_each_required_key_and_where_it_is_missing():
    data = {
        "runs": [
            {"name": "a", "leaf": {}, "leaves": [{"need": "x"}, {"has": 1}]},
            {"long-name": "b", "by-key": {"k": {"maybe": "y"}}, "raw": {}},
        ]
    }
    assert missing_keys(_File, data) == [
        "runs 'a': missing required key 'long-name'",
        "runs 'a'.leaf: missing required key 'need'",
        "runs 'a'.leaves[1]: missing required key 'need'",
        "runs[1]: missing required key 'name'",
        "runs[1].by-key.k: missing required key 'need'",
    ]
    assert missing_keys(_File, {"runs": [{}]}) == [
        "runs[0]: missing required keys 'name', 'long-name'"
    ]
    assert missing_keys(_File, {}) == ["missing required key 'runs'"]
    # A value of the wrong shape is left to the typed load.
    assert missing_keys(_File, {"runs": "x"}) == []
    assert missing_keys(_File, {"runs": [{"name": "a", "long-name": "b"}]}) == []


def test_the_typed_load_raises_every_missing_key_at_once():
    with pytest.raises(MissingKeyError) as raised:
        config_from_data(_File, {"runs": [{"name": "a"}, {"long-name": "b"}]})
    assert str(raised.value) == (
        "runs 'a': missing required key 'long-name'; "
        "runs[1]: missing required key 'name'"
    )


_SUITES = [
    (
        PnrSuiteConfig,
        "pnr_config",
        "runs",
        "tool: openroad\n    synth: s\n    platform: p",
    ),
    (
        SynthSuiteConfig,
        "synth_config",
        "syntheses",
        "model: m\n    model_path: m.yaml\n    tool: yosys",
    ),
    (PowerSuiteConfig, "power_config", "runs", "pnr: p"),
    (
        FpvSuiteConfig,
        "fpv_config",
        "verifications",
        "model: m\n    model_path: m.yaml\n    tool: sby",
    ),
    (FpgaSuiteConfig, "fpga_config", "runs", "model: m\n    model_path: m.yaml"),
    (LintSuiteConfig, "lint_config", "checks", "model: m\n    model_path: m.yaml"),
    (
        CdcSuiteConfig,
        "cdc_config",
        "analyses",
        "model: m\n    model_path: m.yaml\n    tool: cdc\n    constraints: c.sdc",
    ),
]


@pytest.mark.parametrize(
    "loader, filetype, entries, fields",
    _SUITES,
    ids=[filetype for _l, filetype, _e, _f in _SUITES],
)
def test_a_run_without_desc_fails_to_load_naming_the_run_and_the_key(
    tmp_path, loader, filetype, entries, fields
):
    path = tmp_path / "suite.yaml"
    path.write_text(
        f"rtl-buddy-filetype: {filetype}\n{entries}:\n  - name: blk\n    {fields}\n"
    )

    with pytest.raises(FatalRtlBuddyError, match="failed to load") as raised:
        loader(str(path))

    assert isinstance(raised.value.__cause__, MissingKeyError)
    assert str(raised.value.__cause__) == (
        f"{entries} 'blk': missing required key 'desc'"
    )


def test_a_test_without_desc_fails_to_load_naming_the_test_and_the_key(tmp_path):
    path = tmp_path / "tests.yaml"
    path.write_text(
        dedent("""\
            rtl-buddy-filetype: test_config
            testbenches:
              - name: tb
                desc: a bench
                filelist: tb.f
            tests:
              - name: t0
                testbench: tb
            """)
    )

    with pytest.raises(FatalRtlBuddyError, match="failed to load") as raised:
        SuiteConfig(str(path))

    assert str(raised.value.__cause__) == (
        "tests 't0': missing required keys 'desc', 'model', 'model_path'"
    )
