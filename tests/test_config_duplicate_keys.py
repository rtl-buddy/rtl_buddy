"""Duplicate YAML mapping keys in user config are rejected (rtl_buddy#760)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from rtl_buddy.config import ModelConfigLoader, RootConfig, SuiteConfig
from rtl_buddy.config.yaml_loader import load_yaml
from rtl_buddy.config.xplr import load_xplr_config
from rtl_buddy.errors import FatalRtlBuddyError

_PDK_DUP = dedent("""\

    cfg-pdks:
      - name: "nangate45"
        dont-use-cells: ["*probe*"]
        corners:
          typ: "pdk/lib/typ.lib"
        dont-use-cells: ["FILLCELL*"]
    """)


def test_duplicate_top_level_key_names_key_and_both_lines():
    with pytest.raises(FatalRtlBuddyError) as exc:
        load_yaml("a: 1\nb: 2\na: 3\n", "cfg.yaml")
    assert str(exc.value) == (
        "cfg.yaml: duplicate key 'a' at line 3 (first defined at line 1); "
        "YAML would keep only the last value"
    )


def test_duplicate_nested_key_in_a_list_entry():
    with pytest.raises(
        FatalRtlBuddyError,
        match=r"duplicate key 'dont-use-cells' at line 7 \(first defined at line 4\)",
    ):
        load_yaml(_PDK_DUP, "root_config.yaml")


def test_same_key_in_sibling_mappings_is_fine():
    data = load_yaml("a: {x: 1}\nb: {x: 2}\nl:\n  - x: 1\n  - x: 2\n")
    assert data == {"a": {"x": 1}, "b": {"x": 2}, "l": [{"x": 1}, {"x": 2}]}


def test_merge_key_overridden_locally_is_allowed():
    text = dedent("""\
        base: &base
          opts: [a]
          seed: 1
        derived:
          <<: *base
          seed: 2
        """)
    assert load_yaml(text)["derived"] == {"opts": ["a"], "seed": 2}


def test_yaml_1_1_semantics_are_unchanged():
    # `on:` is still the boolean True, so `on:` and `true:` collide.
    assert load_yaml("on: 1\n") == {True: 1}
    with pytest.raises(FatalRtlBuddyError, match="duplicate key 'true' at line 2"):
        load_yaml("on: 1\ntrue: 2\n")


def test_root_config_with_duplicate_pdk_key_fails_to_load(minimal_project: Path):
    cfg = minimal_project / "root_config.yaml"
    cfg.write_text(cfg.read_text() + _PDK_DUP)
    with pytest.raises(FatalRtlBuddyError) as exc:
        RootConfig(name="dup")
    cause = str(exc.value.__cause__)
    assert "duplicate key 'dont-use-cells'" in cause
    assert str(cfg) in cause or "root_config.yaml" in cause


def test_xplr_reader_rejects_duplicate_keys(minimal_project: Path):
    cfg = minimal_project / "root_config.yaml"
    cfg.write_text(
        cfg.read_text()
        + "cfg-xplr:\n  commit-mode: auto\n  commit-mode: self-managed\n"
    )
    with pytest.raises(FatalRtlBuddyError, match="duplicate key 'commit-mode'"):
        load_xplr_config(minimal_project)


def test_suite_with_duplicate_key_fails_to_load(minimal_project: Path):
    suite = minimal_project / "tests.yaml"
    suite.write_text(suite.read_text() + "rtl-buddy-filetype: test_config\n")
    with pytest.raises(FatalRtlBuddyError) as exc:
        SuiteConfig(str(suite))
    assert "duplicate key 'rtl-buddy-filetype'" in str(exc.value.__cause__)


def test_normal_configs_still_load(minimal_project: Path):
    RootConfig(name="ok")
    assert SuiteConfig(str(minimal_project / "tests.yaml")).tests
    assert ModelConfigLoader(str(minimal_project / "models.yaml")).models
