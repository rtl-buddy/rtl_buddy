"""Tests for ``rb verible lint/format --model`` and the exclude machinery.

Covers:
- ``VlogFilelist.extract_source_files``: bare sources only, ``-F`` unrolled,
  ``-v``/``-y``/``+incdir+``/``+define+``/``+libext+`` dropped, deduplicated.
- ``--model`` expansion feeds the verible binary the model's source files.
- cfg-verible ``exclude`` globs and ``--exclude`` both filter the expansion;
  ``*`` crosses directory separators (fnmatch semantics).
- Unknown options pass through to the binary without a ``--`` separator.
- ``--exclude`` without ``--model`` warns and has no effect.
- An expansion left empty by excludes is fatal.
- ``extra_args`` are applied per command uniformly (``format`` included).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from typer.testing import CliRunner

from rtl_buddy.config.model import ModelConfig
from rtl_buddy.config.verible import VeribleConfig
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.tools.verible import Verible
from rtl_buddy.tools.vlog_filelist import VlogFilelist


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_verible_model_lint")


# --- VlogFilelist.extract_source_files (unit) -----------------------------


def test_extract_source_files_bare_only(tmp_path: Path):
    """Bare entries survive in order; library files and directives do not."""
    (tmp_path / "src").mkdir()
    for name in ("a.sv", "b.sv", "lib.sv"):
        (tmp_path / "src" / name).write_text("module m; endmodule\n")
    (tmp_path / "inc").mkdir()

    model = ModelConfig(
        name="m",
        filelist=[
            "src/a.sv",
            "-v src/lib.sv",
            "+incdir+inc",
            "+define+FOO=1",
            "+libext+.sv",
            "src/b.sv",
            "src/a.sv",  # duplicate
        ],
        path=str(tmp_path / "models.yaml"),
    )
    fl = VlogFilelist(name="t", model_cfg=None, output_path=None)
    files = fl.extract_source_files(model)
    assert [os.path.basename(f) for f in files] == ["a.sv", "b.sv"]
    assert all(os.path.isabs(f) for f in files)


def test_extract_source_files_unrolls_dash_F(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.sv").write_text("module i; endmodule\n")
    (tmp_path / "sub" / "inner.f").write_text("inner.sv\n")
    (tmp_path / "top.sv").write_text("module t; endmodule\n")

    model = ModelConfig(
        name="m",
        filelist=["-F sub/inner.f", "top.sv"],
        path=str(tmp_path / "models.yaml"),
    )
    fl = VlogFilelist(name="t", model_cfg=None, output_path=None)
    files = fl.extract_source_files(model)
    assert [os.path.basename(f) for f in files] == ["inner.sv", "top.sv"]


# --- fake verible install --------------------------------------------------


def _install_fake_verible(project: Path) -> Path:
    """Create a fake verible install dir whose binaries record their argv."""
    fakebin = project / "fakebin"
    fakebin.mkdir()
    for exe in (
        "verible-verilog-lint",
        "verible-verilog-format",
        "verible-verilog-syntax",
    ):
        script = fakebin / exe
        script.write_text(
            f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{project / (exe + ".argv")}"\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    # Point cfg-verible at it.
    rc = project / "root_config.yaml"
    rc.write_text(rc.read_text().replace('path: "/usr/bin"', 'path: "fakebin"'))
    return fakebin


def _argv(project: Path, exe: str = "verible-verilog-lint") -> list[str]:
    return (project / f"{exe}.argv").read_text().splitlines()


def _add_csr_model(project: Path) -> None:
    """Add a model mixing bare, generated-pattern, and library entries."""
    gen = project / "src" / "gen"
    gen.mkdir()
    (gen / "example_csr_pkg.sv").write_text("package p; endpackage\n")
    (project / "src" / "lib_cell.sv").write_text("module l; endmodule\n")
    (project / "src" / "wrap.sv").write_text("module w; endmodule\n")
    models = project / "models.yaml"
    models.write_text(
        models.read_text()
        + """
  - name: csr
    desc: mixed model for --model expansion tests
    filelist:
      - src/gen/example_csr_pkg.sv
      - -v src/lib_cell.sv
      - src/wrap.sv
"""
    )


# --- rb verible lint --model (integration through Typer) ------------------


def test_rb_verible_lint_model_expands_files(minimal_project: Path):
    """``--model`` appends the model's bare sources; ``-v`` entries dropped."""
    _install_fake_verible(minimal_project)
    _add_csr_model(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["verible", "lint", "--model", "csr"])
    assert result.exit_code == 0, result.output
    argv = _argv(minimal_project)
    assert "src/gen/example_csr_pkg.sv" in argv
    assert "src/wrap.sv" in argv
    assert "src/lib_cell.sv" not in argv


def test_rb_verible_lint_model_cli_exclude(minimal_project: Path):
    """``--exclude`` globs match project-root-relative paths; ``*`` crosses
    directory separators."""
    _install_fake_verible(minimal_project)
    _add_csr_model(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["verible", "lint", "--model", "csr", "--exclude", "*_csr_pkg.sv"],
    )
    assert result.exit_code == 0, result.output
    argv = _argv(minimal_project)
    assert "src/gen/example_csr_pkg.sv" not in argv
    assert "src/wrap.sv" in argv


def test_rb_verible_lint_model_cfg_exclude(minimal_project: Path):
    """cfg-verible ``exclude`` filters the expansion without any CLI flag."""
    _install_fake_verible(minimal_project)
    _add_csr_model(minimal_project)
    rc = minimal_project / "root_config.yaml"
    rc.write_text(
        rc.read_text().replace(
            "    extra_args: {}",
            '    extra_args: {}\n    exclude: ["*_csr_pkg.sv"]',
        )
    )
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["verible", "lint", "--model", "csr"])
    assert result.exit_code == 0, result.output
    argv = _argv(minimal_project)
    assert "src/gen/example_csr_pkg.sv" not in argv
    assert "src/wrap.sv" in argv


def test_rb_verible_lint_unknown_option_passes_through(minimal_project: Path):
    """Verible's own flags need no ``--`` separator in front of them."""
    _install_fake_verible(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["verible", "lint", "--rules_config_search", "src/example.sv"],
    )
    assert result.exit_code == 0, result.output
    argv = _argv(minimal_project)
    assert "--rules_config_search" in argv
    assert "src/example.sv" in argv


def test_rb_verible_lint_exclude_without_model_warns(minimal_project: Path):
    """``--exclude`` alone filters nothing — warn instead of silently no-op."""
    _install_fake_verible(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["verible", "lint", "--exclude", "*.sv", "src/example.sv"],
    )
    assert result.exit_code == 0, result.output
    assert "--exclude only filters --model expansion" in result.output
    assert "src/example.sv" in _argv(minimal_project)


def test_rb_verible_lint_model_all_excluded_is_fatal(minimal_project: Path):
    _install_fake_verible(minimal_project)
    _add_csr_model(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["verible", "lint", "--model", "csr", "--exclude", "*"],
    )
    assert result.exit_code != 0
    assert not (minimal_project / "verible-verilog-lint.argv").exists()


def test_rb_verible_lint_unknown_model_is_fatal(minimal_project: Path):
    _install_fake_verible(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["verible", "lint", "--model", "not_a_real_model"])
    assert result.exit_code != 0


def test_rb_verible_format_model_expands_files(minimal_project: Path):
    """``format --model`` shares the lint expansion path."""
    _install_fake_verible(minimal_project)
    _add_csr_model(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["verible", "format", "--model", "csr"])
    assert result.exit_code == 0, result.output
    argv = _argv(minimal_project, "verible-verilog-format")
    assert "src/wrap.sv" in argv
    assert "src/lib_cell.sv" not in argv


# --- extra_args uniformity (unit) -----------------------------------------


def _capture_verible(extra_args: dict[str, list[str]]) -> tuple[Verible, list]:
    cfg = VeribleConfig(
        name="t", path="/nonexistent", extra_args=extra_args, available=True
    )
    ver = Verible("t/verible", cfg=cfg)
    calls: list[tuple[str, list[str]]] = []
    ver.do_exe = lambda exe, args: calls.append((exe, args)) or 0
    return ver, calls


def test_extra_args_applied_to_format():
    """A configured ``extra_args: {format: [...]}`` block is honoured — it
    used to be silently ignored (only ``lint`` applied its extra_args)."""
    ver, calls = _capture_verible({"format": ["--column_limit=130"]})
    ver.do_cmd(cmd="format", verible_args=["f.sv"])
    assert calls == [("verible-verilog-format", ["--column_limit=130", "f.sv"])]


def test_extra_args_applied_to_lint_once():
    ver, calls = _capture_verible({"lint": ["--rules=-no-tabs"]})
    ver.do_cmd(cmd="lint", verible_args=["f.sv"])
    assert calls == [("verible-verilog-lint", ["--rules=-no-tabs", "f.sv"])]


def test_extra_args_lead_so_cli_wins():
    """Configured args come first: for repeated gflags the later (CLI)
    occurrence wins inside verible."""
    ver, calls = _capture_verible({"lint": ["--rules=-no-tabs"]})
    ver.do_cmd(cmd="lint", verible_args=["--rules=+no-tabs", "f.sv"])
    assert calls[0][1] == ["--rules=-no-tabs", "--rules=+no-tabs", "f.sv"]
