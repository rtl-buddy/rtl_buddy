"""Tests for the style-lint flow: ``lint.yaml`` / ``lint_regression.yaml``,
``rb lint`` / ``rb lint-regression``, and the graph flow stamp.

Covers:
- Suite/regression config loading, duplicate names, unknown check names.
- The runner: model expansion (library entries dropped), cfg-verible +
  per-check exclude globs, violation counting from **stderr** (where
  verible-verilog-lint writes findings), the tool-error branch, and the
  ``lint.f`` / ``lint.log`` artefacts.
- xfail / xfail_strict remapping and the reglvl skip.
- ``rb lint`` (all / named / --list) and ``rb lint-regression``
  (./lint_regression.yaml filename convention), exit codes.
- ``cfg-rtl-reg: lint-reg-cfg-path`` resolution.
- The graph config tier stamps lint suites/runs with ``flow: lint``.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.config.lint import LintRegConfig, LintSuiteConfig
from rtl_buddy.config.root import (
    REG_CFG_PATH_KEYS,
    load_reg_cfg_paths,
    resolve_reg_cfg_path,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.graph import build_config_tier
from rtl_buddy.rtl_buddy import RtlBuddy


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_lint_flow")


#: Fake verible-verilog-lint: greps each file argument for the marker
#: ``VIOLATION`` and reports one finding line per hit **to stderr**, with
#: exit code 1 when anything was found — the real tool's convention.
_FAKE_LINT = """#!/bin/sh
found=0
for f in "$@"; do
  case "$f" in --*) continue;; esac
  n=1
  while IFS= read -r line; do
    case "$line" in
      *VIOLATION*)
        echo "$f:$n:1: fake finding [Style: fake] [fake-rule]" >&2
        found=1
        ;;
    esac
    n=$((n+1))
  done < "$f"
done
exit $found
"""


def _install_fake_verible(project: Path) -> None:
    fakebin = project / "fakebin"
    fakebin.mkdir()
    lint = fakebin / "verible-verilog-lint"
    lint.write_text(_FAKE_LINT)
    lint.chmod(lint.stat().st_mode | stat.S_IXUSR)
    syntax = fakebin / "verible-verilog-syntax"
    syntax.write_text("#!/bin/sh\nexit 0\n")
    syntax.chmod(syntax.stat().st_mode | stat.S_IXUSR)
    rc = project / "root_config.yaml"
    rc.write_text(rc.read_text().replace('path: "/usr/bin"', 'path: "fakebin"'))


def _write_suite(
    project: Path,
    *,
    check_yaml: str,
    suite_rel: str = "lint",
) -> Path:
    suite_dir = project / suite_rel
    suite_dir.mkdir(parents=True, exist_ok=True)
    cfg = suite_dir / "lint.yaml"
    cfg.write_text(f"rtl-buddy-filetype: lint_config\n\nchecks:\n{check_yaml}")
    return cfg


_BASIC_CHECK = """  - name: example_style
    desc: example model style
    model: example
    model_path: ../models.yaml
"""


# --- config loading -------------------------------------------------------


def test_suite_config_loads_and_lists_checks(minimal_project: Path):
    cfg = _write_suite(minimal_project, check_yaml=_BASIC_CHECK)
    suite = LintSuiteConfig(str(cfg))
    assert suite.get_check_names() == ["example_style"]
    (check,) = suite.get_checks("example_style")
    assert check.get_top() == "example"
    assert check.get_tool_name() == "verible"
    assert check.get_reglvl() == 0


def test_suite_config_duplicate_check_is_fatal(minimal_project: Path):
    cfg = _write_suite(minimal_project, check_yaml=_BASIC_CHECK + _BASIC_CHECK)
    with pytest.raises(FatalRtlBuddyError, match="duplicate check name"):
        LintSuiteConfig(str(cfg))


def test_suite_config_unknown_check_is_fatal(minimal_project: Path):
    cfg = _write_suite(minimal_project, check_yaml=_BASIC_CHECK)
    suite = LintSuiteConfig(str(cfg))
    with pytest.raises(FatalRtlBuddyError, match="not found"):
        suite.get_checks("nope")


def test_reg_config_loads_suites(minimal_project: Path):
    _write_suite(minimal_project, check_yaml=_BASIC_CHECK)
    reg = minimal_project / "lint_regression.yaml"
    reg.write_text(
        "rtl-buddy-filetype: lint_reg_config\nlint-configs: [lint/lint.yaml]\n"
    )
    cfg = LintRegConfig(name="t", path=str(reg))
    assert [s.get_check_names() for s in cfg.get_suite_configs()] == [["example_style"]]


def test_lint_reg_cfg_path_key_resolves(tmp_path: Path):
    assert REG_CFG_PATH_KEYS["lint"] == ("lint-reg-cfg-path", "lint_path")
    rc = tmp_path / "root_config.yaml"
    rc.write_text(
        "cfg-rtl-reg:\n"
        '  reg-cfg-path: "regression.yaml"\n'
        '  lint-reg-cfg-path: "ci/lint_regression.yaml"\n'
    )
    resolved = resolve_reg_cfg_path(load_reg_cfg_paths(rc), rc, "lint")
    assert resolved == str(tmp_path / "ci" / "lint_regression.yaml")


# --- rb lint --------------------------------------------------------------


def test_rb_lint_clean_passes(minimal_project: Path):
    _install_fake_verible(minimal_project)
    _write_suite(minimal_project, check_yaml=_BASIC_CHECK)
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["lint", "-c", "lint/lint.yaml"])
    assert result.exit_code == 0, result.output
    # Artefacts: the expanded file set and the tool log.
    art = minimal_project / "lint" / "artefacts" / "example_style"
    assert "src/example.sv" in (art / "lint.f").read_text()
    assert (art / "lint.log").is_file()


def test_rb_lint_violations_fail_and_are_counted(minimal_project: Path):
    _install_fake_verible(minimal_project)
    (minimal_project / "src" / "bad.sv").write_text(
        "module bad; // VIOLATION\nwire x; // VIOLATION\nendmodule\n"
    )
    models = minimal_project / "models.yaml"
    models.write_text(
        models.read_text()
        + "  - name: bad\n    desc: bad model\n    filelist:\n      - src/bad.sv\n"
    )
    _write_suite(
        minimal_project,
        check_yaml=(
            "  - name: bad_style\n"
            "    desc: has violations\n"
            "    model: bad\n"
            "    model_path: ../models.yaml\n"
        ),
    )
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["lint", "-c", "lint/lint.yaml"])
    assert result.exit_code == 1
    # Findings come back on stderr and are still counted.
    assert "2" in result.output  # violations column


def test_rb_lint_excludes_filter_expansion(minimal_project: Path):
    _install_fake_verible(minimal_project)
    (minimal_project / "src" / "gen_csr_pkg.sv").write_text(
        "package p; // VIOLATION\nendpackage\n"
    )
    models = minimal_project / "models.yaml"
    models.write_text(
        models.read_text()
        + "  - name: mixed\n    desc: mixed\n    filelist:\n"
        + "      - src/example.sv\n      - src/gen_csr_pkg.sv\n"
    )
    _write_suite(
        minimal_project,
        check_yaml=(
            "  - name: mixed_style\n"
            "    desc: generated excluded\n"
            "    model: mixed\n"
            "    model_path: ../models.yaml\n"
            '    exclude: ["*_csr_pkg.sv"]\n'
        ),
    )
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["lint", "-c", "lint/lint.yaml"])
    assert result.exit_code == 0, result.output
    listing = (
        minimal_project / "lint" / "artefacts" / "mixed_style" / "lint.f"
    ).read_text()
    assert "gen_csr_pkg.sv" not in listing


def test_rb_lint_xfail_turns_fail_into_pass(minimal_project: Path):
    _install_fake_verible(minimal_project)
    (minimal_project / "src" / "bad.sv").write_text("// VIOLATION\n")
    models = minimal_project / "models.yaml"
    models.write_text(
        models.read_text()
        + "  - name: bad\n    desc: bad\n    filelist:\n      - src/bad.sv\n"
    )
    _write_suite(
        minimal_project,
        check_yaml=(
            "  - name: debt\n"
            "    desc: tracked debt\n"
            "    model: bad\n"
            "    model_path: ../models.yaml\n"
            "    xfail: true\n"
        ),
    )
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["lint", "-c", "lint/lint.yaml"])
    assert result.exit_code == 0, result.output
    assert "XFAIL" in result.output


def test_rb_lint_xfail_strict_fails_on_unexpected_pass(minimal_project: Path):
    _install_fake_verible(minimal_project)
    _write_suite(
        minimal_project,
        check_yaml=_BASIC_CHECK + "    xfail_strict: true\n",
    )
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["lint", "-c", "lint/lint.yaml"])
    assert result.exit_code == 1
    assert "XPASS" in result.output


def test_rb_lint_tool_error_is_a_fail_not_zero_violations(minimal_project: Path):
    _install_fake_verible(minimal_project)
    lint = minimal_project / "fakebin" / "verible-verilog-lint"
    lint.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    _write_suite(minimal_project, check_yaml=_BASIC_CHECK)
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["lint", "-c", "lint/lint.yaml"])
    assert result.exit_code == 1
    assert "code 3" in result.output


def test_rb_lint_list(minimal_project: Path):
    _install_fake_verible(minimal_project)
    _write_suite(minimal_project, check_yaml=_BASIC_CHECK)
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["lint", "-c", "lint/lint.yaml", "--list"])
    assert result.exit_code == 0, result.output
    assert "example_style" in result.output


# --- rb lint-regression ---------------------------------------------------


def _write_regression(minimal_project: Path, extra_check: str = "") -> None:
    _write_suite(minimal_project, check_yaml=_BASIC_CHECK + extra_check)
    (minimal_project / "lint_regression.yaml").write_text(
        "rtl-buddy-filetype: lint_reg_config\nlint-configs: [lint/lint.yaml]\n"
    )


def test_rb_lint_regression_filename_convention(minimal_project: Path):
    _install_fake_verible(minimal_project)
    _write_regression(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["lint-regression"])
    assert result.exit_code == 0, result.output
    assert "example_style" in result.output


def test_rb_lint_regression_reglvl_skips(minimal_project: Path):
    _install_fake_verible(minimal_project)
    _write_regression(
        minimal_project,
        extra_check=(
            "  - name: deep_style\n"
            "    desc: only at level 2\n"
            "    model: example\n"
            "    model_path: ../models.yaml\n"
            "    reglvl: 2\n"
        ),
    )
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["lint-regression"])
    assert result.exit_code == 0, result.output
    assert "SKIP" in result.output
    result = runner.invoke(rb.app, ["lint-regression", "-l", "2"])
    assert result.exit_code == 0, result.output
    assert "SKIP" not in result.output


# --- graph flow stamp -----------------------------------------------------


def test_graph_config_tier_stamps_lint_flow(minimal_project: Path):
    _install_fake_verible(minimal_project)
    _write_regression(minimal_project)
    graph = build_config_tier(minimal_project)
    suites = {n["id"]: n for n in graph["nodes"] if n["type"] == "suite"}
    tests = {n["id"]: n for n in graph["nodes"] if n["type"] == "test"}
    assert suites["suite:lint"]["flow"] == "lint"
    run = tests["test:lint#example_style"]
    assert run["flow"] == "lint"
    assert run["tool"] == "verible"
    assert run["toplevel"] == "example"
    targets = {
        (link["source"], link["target"])
        for link in graph["links"]
        if link["type"] == "targets"
    }
    assert ("test:lint#example_style", "module:example") in targets
