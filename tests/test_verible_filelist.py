"""Tests for ``rb verible filelist`` and the underlying generator.

Covers:
- Multi-model union (default): aggregates every model under the project root.
- ``--model`` filter: emits only the selected models' transitive sources.
- ``-o`` override: writes to a non-default path.
- Verible filelist format: bare source paths + ``+incdir+`` only; ``-y``/``-v``
  and ``+libext+`` are dropped because verible-verilog-ls silently ignores
  them (see verible's ``verilog-filelist.cc::AppendFileListFromContent``).
- ``-F`` chains are flattened so the LSP doesn't need to follow indirection.
- Duplicate paths across models are deduplicated.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.config.model import ModelConfig
from rtl_buddy.errors import FilelistError
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.tools.vlog_filelist import VlogFilelist


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_verible_filelist")


# --- VlogFilelist.write_verible_filelist (unit) ---------------------------


def test_verible_filelist_drops_unsupported_directives(tmp_path: Path):
    """``-v`` / ``-y`` / ``+libext+`` get filtered out; only paths and
    ``+incdir+`` survive, matching what verible-verilog-ls actually parses."""
    src = tmp_path / "src" / "a.sv"
    src.parent.mkdir()
    src.write_text("module a; endmodule\n")
    libdir = tmp_path / "lib"
    libdir.mkdir()
    incdir = tmp_path / "include"
    incdir.mkdir()
    libfile = libdir / "b.sv"
    libfile.write_text("module b; endmodule\n")

    model = ModelConfig(
        name="m",
        filelist=[
            "src/a.sv",
            "-y lib/",
            "+incdir+include",
            "+libext+.sv+.v",
            "-v lib/b.sv",
        ],
        path=str(tmp_path / "models.yaml"),
    )

    out = tmp_path / "verible.filelist"
    fl = VlogFilelist(name="t", model_cfg=None, output_path=str(out))
    fl.write_verible_filelist([model])

    text = out.read_text()
    assert "src/a.sv" in text
    assert "+incdir+include" in text
    # Filtered:
    assert "-y" not in text
    assert "-v" not in text
    assert "+libext+" not in text


def test_verible_filelist_unrolls_dash_F_chains(tmp_path: Path):
    """``-F sub.f`` references are inlined so the LSP only sees flat paths."""
    src = tmp_path / "src" / "x.sv"
    src.parent.mkdir()
    src.write_text("module x; endmodule\n")
    other = tmp_path / "src" / "y.sv"
    other.write_text("module y; endmodule\n")

    sub_f = tmp_path / "src" / "sub.f"
    sub_f.write_text("y.sv\n")

    model = ModelConfig(
        name="m",
        filelist=["src/x.sv", "-F src/sub.f"],
        path=str(tmp_path / "models.yaml"),
    )

    out = tmp_path / "verible.filelist"
    fl = VlogFilelist(name="t", model_cfg=None, output_path=str(out))
    fl.write_verible_filelist([model])

    text = out.read_text()
    assert "src/x.sv" in text
    assert "src/y.sv" in text
    # The inlined .f reference itself must not appear as a -F line.
    assert "-F" not in text


def test_verible_filelist_deduplicates_across_models(tmp_path: Path):
    """When two models share a common source, it appears once in the output."""
    shared = tmp_path / "src" / "shared.sv"
    shared.parent.mkdir()
    shared.write_text("module shared; endmodule\n")
    only_a = tmp_path / "src" / "a_only.sv"
    only_a.write_text("module a_only; endmodule\n")
    only_b = tmp_path / "src" / "b_only.sv"
    only_b.write_text("module b_only; endmodule\n")

    model_a = ModelConfig(
        name="a",
        filelist=["src/shared.sv", "src/a_only.sv"],
        path=str(tmp_path / "models.yaml"),
    )
    model_b = ModelConfig(
        name="b",
        filelist=["src/shared.sv", "src/b_only.sv"],
        path=str(tmp_path / "models.yaml"),
    )

    out = tmp_path / "verible.filelist"
    fl = VlogFilelist(name="t", model_cfg=None, output_path=str(out))
    fl.write_verible_filelist([model_a, model_b])

    lines = [
        ln for ln in out.read_text().splitlines() if ln and not ln.startswith("//")
    ]
    assert lines.count("src/shared.sv") == 1
    assert "src/a_only.sv" in lines
    assert "src/b_only.sv" in lines


def test_verible_filelist_empty_model_list_errors(tmp_path: Path):
    fl = VlogFilelist(name="t", model_cfg=None, output_path=str(tmp_path / "out.f"))
    with pytest.raises(FilelistError):
        fl.write_verible_filelist([])


# --- rb verible filelist (integration through Typer) ---------------------


def test_rb_verible_filelist_default_writes_at_project_root(minimal_project: Path):
    """Default ``rb verible filelist`` writes ``<project_root>/verible.filelist``
    with every model's sources unioned. Mirrors the LSP auto-discovery layout."""
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["verible", "filelist"])
    assert result.exit_code == 0, result.output
    out = minimal_project / "verible.filelist"
    assert out.is_file()
    text = out.read_text()
    assert "rtl-buddy generated verible filelist" in text
    # The fixture has a single model ("example") with src/example.sv.
    assert "src/example.sv" in text


def test_rb_verible_filelist_model_filter(minimal_project: Path):
    """``--model NAME`` restricts the output to the named model only."""
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["verible", "filelist", "--model", "example"])
    assert result.exit_code == 0, result.output
    text = (minimal_project / "verible.filelist").read_text()
    assert "src/example.sv" in text


def test_rb_verible_filelist_unknown_model_exits_nonzero(minimal_project: Path):
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["verible", "filelist", "--model", "not_a_real_model"]
    )
    assert result.exit_code != 0


def test_rb_verible_filelist_output_override(minimal_project: Path, tmp_path: Path):
    """``-o PATH`` writes to a non-default location and skips the default
    ``<project_root>/verible.filelist`` path entirely."""
    runner, rb = _runner()
    custom = minimal_project / "custom_dir" / "my.filelist"
    custom.parent.mkdir()
    result = runner.invoke(rb.app, ["verible", "filelist", "-o", str(custom)])
    assert result.exit_code == 0, result.output
    assert custom.is_file()
    assert not (minimal_project / "verible.filelist").exists()
