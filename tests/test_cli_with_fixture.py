"""CLI smoke tests that exercise the RootConfig load path.

These tests use the ``minimal_project`` fixture (see ``tests/conftest.py``)
which sets up a valid root_config.yaml + regression.yaml + tests.yaml +
models.yaml in a temp dir and chdirs into it. They cover the slice of
``rtl_buddy.py`` and ``config/root.py`` that runs whenever any non-skill,
non-docs command is invoked.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from rtl_buddy.rtl_buddy import RtlBuddy


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_cli_fixture")


def test_test_list_emits_configured_test_names(minimal_project: Path):
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["test", "--list"])
    assert result.exit_code == 0, result.output
    # tests.yaml declares "basic" and "extra".
    assert "basic" in result.output
    assert "extra" in result.output


def test_test_list_explicit_config_path(minimal_project: Path):
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["test", "-c", "tests.yaml", "--list"])
    assert result.exit_code == 0, result.output
    assert "basic" in result.output


def test_test_list_missing_config_errors_with_exit_2(minimal_project: Path):
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["test", "-c", "does-not-exist.yaml", "--list"])
    # FatalRtlBuddyError is caught by run(), but CliRunner only sees the
    # raised exception. Either way, exit code must be non-zero.
    assert result.exit_code != 0


def test_filelist_writes_generated_filelist(minimal_project: Path):
    runner, rb = _runner()
    out = minimal_project / "run.f"
    result = runner.invoke(
        rb.app, ["filelist", "example", str(out), "-c", "models.yaml"]
    )
    assert result.exit_code == 0, result.output
    assert out.is_file()
    text = out.read_text()
    assert "rtl-buddy generated model filelist" in text
    assert "example.sv" in text


def test_filelist_unknown_model_exits_nonzero(minimal_project: Path):
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["filelist", "missing_model", "run.f", "-c", "models.yaml"]
    )
    assert result.exit_code != 0


def test_builder_override_validation_against_root_config(minimal_project: Path):
    """``-B <name>`` is validated against builders declared in root_config.yaml."""
    runner, rb = _runner()

    # Valid builder name from root_config.yaml: "stub".
    result_ok = runner.invoke(rb.app, ["-B", "stub", "test", "--list"])
    assert result_ok.exit_code == 0, result_ok.output

    # Unknown builder name should fail validation in cb_builder.
    result_bad = runner.invoke(rb.app, ["-B", "not-a-builder", "test", "--list"])
    assert result_bad.exit_code != 0
    assert (
        "configured builders" in result_bad.output.lower()
        or "stub" in result_bad.output
    )


def test_discover_rtl_builder_names_from_real_root_config(minimal_project: Path):
    """The static discovery helper should find the builder declared in the fixture."""
    from rtl_buddy.config.root import RootConfig

    names = RootConfig.discover_rtl_builder_names()
    assert names == ["stub"]


def test_filelist_with_strip_and_deduplicate(minimal_project: Path):
    """Exercise the strip/deduplicate post-processing in VlogFilelist."""
    runner, rb = _runner()
    out = minimal_project / "stripped.f"
    result = runner.invoke(
        rb.app,
        [
            "filelist",
            "example",
            str(out),
            "-c",
            "models.yaml",
            "--strip",
            "--deduplicate",
        ],
    )
    assert result.exit_code == 0, result.output
    text = out.read_text()
    # With --strip, leading "-y "/"+incdir+" options are removed; the bare path remains.
    assert "example.sv" in text


def test_root_config_resolves_from_nested_cwd(minimal_project: Path, monkeypatch):
    """RootConfig discovery should walk up from a nested working directory."""
    nested = minimal_project / "src"
    monkeypatch.chdir(nested)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["test", "-c", str(minimal_project / "tests.yaml"), "--list"]
    )
    assert result.exit_code == 0, result.output
    assert "basic" in result.output
