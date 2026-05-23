"""Tests for the ``rb hier`` command + ``RtlBuddyView`` tool wrapper.

The real ``rtl-buddy-view`` binary is not on PATH in CI, so these
tests stub it with a tiny shell script that records its argv and
exits with a controllable status. This pins the CLI shape we promise
to the downstream viewer (``--top``, ``--filelist``, ``--format``,
``--output``, ``--frontend``, ``--cdc-annotations``, ``--rdc-annotations``,
``--clock-legend``).
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.config.model import ModelConfig
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.tools.hier_rtl_buddy_view import RtlBuddyView


def _make_fake_view(
    tmp_path: Path, *, exit_code: int = 0, name: str = "rtl-buddy-view"
) -> tuple[Path, Path]:
    """Drop a fake ``rtl-buddy-view`` that records argv to a JSON sidecar."""
    record = tmp_path / f"{name}-argv.json"
    script = tmp_path / name
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'python - "$@" <<PY\n'
        "import json, sys\n"
        f'open({json.dumps(str(record))}, "w").write(json.dumps(sys.argv[1:]))\n'
        "PY\n"
        f"exit {exit_code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script, record


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_hier")


# --- RtlBuddyView wrapper (unit) ------------------------------------------


def test_wrapper_builds_expected_argv_and_filelist(tmp_path: Path):
    src = tmp_path / "src" / "example.sv"
    src.parent.mkdir()
    src.write_text("module example; endmodule\n")
    model = ModelConfig(
        name="example",
        filelist=[str(src)],
        path=str(tmp_path / "models.yaml"),
    )
    script, record = _make_fake_view(tmp_path)

    view = RtlBuddyView(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        format="mermaid",
        executable=str(script),
    )
    assert view.run() == 0

    argv = json.loads(record.read_text())
    assert argv[:2] == ["--top", "example"]
    assert "--filelist" in argv
    fl_idx = argv.index("--filelist") + 1
    assert argv[fl_idx].endswith("hier.f")
    assert Path(argv[fl_idx]).is_file()
    assert "--format" in argv and argv[argv.index("--format") + 1] == "mermaid"


def test_wrapper_forwards_optional_flags(tmp_path: Path):
    src = tmp_path / "src" / "example.sv"
    src.parent.mkdir()
    src.write_text("module example; endmodule\n")
    model = ModelConfig(
        name="example",
        filelist=[str(src)],
        path=str(tmp_path / "models.yaml"),
    )
    cdc_map = tmp_path / "domain_map.json"
    cdc_map.write_text("{}")
    rdc_map = tmp_path / "reset_domain_map.json"
    rdc_map.write_text("{}")
    output_file = tmp_path / "hier.dot"
    script, record = _make_fake_view(tmp_path)

    view = RtlBuddyView(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        format="dot",
        output=str(output_file),
        frontend="slang",
        cdc_annotations=str(cdc_map),
        rdc_annotations=str(rdc_map),
        clock_legend=True,
        executable=str(script),
    )
    assert view.run() == 0

    argv = json.loads(record.read_text())
    # All optional flags are forwarded verbatim.
    assert argv[argv.index("--output") + 1] == str(output_file)
    assert argv[argv.index("--frontend") + 1] == "slang"
    assert argv[argv.index("--cdc-annotations") + 1] == str(cdc_map)
    assert argv[argv.index("--rdc-annotations") + 1] == str(rdc_map)
    assert "--clock-legend" in argv


def test_wrapper_propagates_viewer_exit_code(tmp_path: Path):
    src = tmp_path / "src" / "example.sv"
    src.parent.mkdir()
    src.write_text("module example; endmodule\n")
    model = ModelConfig(
        name="example",
        filelist=[str(src)],
        path=str(tmp_path / "models.yaml"),
    )
    script, _ = _make_fake_view(tmp_path, exit_code=2)
    view = RtlBuddyView(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    assert view.run() == 2


def test_wrapper_rejects_missing_cdc_annotations(tmp_path: Path):
    from rtl_buddy.errors import FatalRtlBuddyError

    src = tmp_path / "src" / "example.sv"
    src.parent.mkdir()
    src.write_text("module example; endmodule\n")
    model = ModelConfig(
        name="example",
        filelist=[str(src)],
        path=str(tmp_path / "models.yaml"),
    )
    script, _ = _make_fake_view(tmp_path)

    view = RtlBuddyView(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        cdc_annotations=str(tmp_path / "missing.json"),
        executable=str(script),
    )
    with pytest.raises(FatalRtlBuddyError):
        view.run()


def test_wrapper_rejects_missing_rdc_annotations(tmp_path: Path):
    from rtl_buddy.errors import FatalRtlBuddyError

    src = tmp_path / "src" / "example.sv"
    src.parent.mkdir()
    src.write_text("module example; endmodule\n")
    model = ModelConfig(
        name="example",
        filelist=[str(src)],
        path=str(tmp_path / "models.yaml"),
    )
    script, _ = _make_fake_view(tmp_path)

    view = RtlBuddyView(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        rdc_annotations=str(tmp_path / "missing.json"),
        executable=str(script),
    )
    with pytest.raises(FatalRtlBuddyError, match="rdc-annotations file not found"):
        view.run()


def test_wrapper_rejects_missing_tool_path(tmp_path: Path):
    """An absolute path that doesn't exist surfaces a friendly error,
    not a subprocess FileNotFoundError traceback. Caught in real-world
    use when rtl-buddy-view lives only in a venv that isn't on PATH."""
    from rtl_buddy.errors import FatalRtlBuddyError

    src = tmp_path / "src" / "example.sv"
    src.parent.mkdir()
    src.write_text("module example; endmodule\n")
    model = ModelConfig(
        name="example",
        filelist=[str(src)],
        path=str(tmp_path / "models.yaml"),
    )
    view = RtlBuddyView(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(tmp_path / "does-not-exist"),
    )
    with pytest.raises(FatalRtlBuddyError, match="not found or not executable"):
        view.run()


def test_wrapper_rejects_missing_tool_on_path(tmp_path: Path, monkeypatch):
    """A bare command name that doesn't resolve through PATH gets the
    same friendly treatment via ``shutil.which``."""
    from rtl_buddy.errors import FatalRtlBuddyError

    src = tmp_path / "src" / "example.sv"
    src.parent.mkdir()
    src.write_text("module example; endmodule\n")
    model = ModelConfig(
        name="example",
        filelist=[str(src)],
        path=str(tmp_path / "models.yaml"),
    )
    # Empty PATH ensures the lookup fails deterministically.
    monkeypatch.setenv("PATH", "")
    view = RtlBuddyView(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable="totally-fake-binary-xyz",
    )
    with pytest.raises(FatalRtlBuddyError, match="not found on PATH"):
        view.run()


# --- rb hier command (integration through Typer) --------------------------


def test_rb_hier_invokes_stubbed_viewer(minimal_project: Path):
    script, record = _make_fake_view(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "hier",
            "example",
            "-c",
            "models.yaml",
            "--format",
            "json",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code == 0, result.output
    argv = json.loads(record.read_text())
    assert argv[argv.index("--top") + 1] == "example"
    assert argv[argv.index("--format") + 1] == "json"
    # Filelist artefact landed under artefacts/hier/<model>/.
    assert (minimal_project / "artefacts" / "hier" / "example" / "hier.f").is_file()


def test_rb_hier_unknown_model_exits_nonzero(minimal_project: Path):
    script, _ = _make_fake_view(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "hier",
            "missing_model",
            "-c",
            "models.yaml",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code != 0


def test_rb_hier_viewer_exit_propagates(minimal_project: Path):
    script, _ = _make_fake_view(minimal_project, exit_code=1)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "hier",
            "example",
            "-c",
            "models.yaml",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code == 1
