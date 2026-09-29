"""Tests for the ``rb hier`` command and the ``RtlBuddyView`` tool wrapper.

``rtl-buddy-view`` is stubbed with a shell script that records its argv and
exits with a controllable status, which pins the CLI shape promised to the viewer.
"""

from __future__ import annotations

import json
import shlex
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.config.model import ModelConfig
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.tools.hier_rtl_buddy_view import RtlBuddyView

# Stubs run a here-doc Python snippet, so capture the real interpreter at import time;
# one test monkeypatches ``sys.executable``.
_PYTHON = shlex.quote(sys.executable)


def _make_fake_view(
    tmp_path: Path, *, exit_code: int = 0, name: str = "rtl-buddy-view"
) -> tuple[Path, Path]:
    """Drop a fake ``rtl-buddy-view`` that records argv to a JSON sidecar."""
    record = tmp_path / f"{name}-argv.json"
    script = tmp_path / name
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'{_PYTHON} - "$@" <<PY\n'
        "import json, sys\n"
        f'open({json.dumps(str(record))}, "w").write(json.dumps(sys.argv[1:]))\n'
        "PY\n"
        f"exit {exit_code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script, record


def _make_old_view(
    tmp_path: Path,
    *,
    version: str | None = "0.7.1",
    option: str = "--block-diagram",
    name: str = "old-view",
) -> Path:
    """A fake viewer that predates ``option``.

    It rejects the unknown option with a stderr message and non-zero exit and, unless
    ``version`` is None, answers ``--version``.
    """
    script = tmp_path / name
    version_branch = (
        f'if [ "$1" = "--version" ]; then\n'
        f'  echo "rtl-buddy-view {version}"\n'
        f"  exit 0\n"
        f"fi\n"
        if version is not None
        else ""
    )
    script.write_text(
        "#!/usr/bin/env bash\n"
        + version_branch
        + f'echo "Error: No such option: {option}" >&2\n'
        "exit 2\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _example_model(tmp_path: Path) -> ModelConfig:
    src = tmp_path / "src" / "example.sv"
    src.parent.mkdir(exist_ok=True)
    src.write_text("module example; endmodule\n")
    return ModelConfig(
        name="example",
        filelist=[str(src)],
        path=str(tmp_path / "models.yaml"),
    )


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_hier")


# RtlBuddyView wrapper (unit)


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


# --block-diagram


def test_wrapper_forwards_block_diagram_when_set(tmp_path: Path):
    """The dot-only block-diagram mode reaches the renderer verbatim."""
    script, record = _make_fake_view(tmp_path)
    view = RtlBuddyView(
        name="t",
        model_cfg=_example_model(tmp_path),
        suite_dir=str(tmp_path),
        format="dot",
        block_diagram=True,
        executable=str(script),
    )
    assert view.run() == 0
    assert "--block-diagram" in json.loads(record.read_text())


def test_wrapper_omits_block_diagram_when_unset(tmp_path: Path):
    """With the flag unset the renderer CLI is unchanged, so `rb hier` works against
    every released viewer."""
    script, record = _make_fake_view(tmp_path)
    view = RtlBuddyView(
        name="t",
        model_cfg=_example_model(tmp_path),
        suite_dir=str(tmp_path),
        format="dot",
        executable=str(script),
    )
    assert view.run() == 0
    assert "--block-diagram" not in json.loads(record.read_text())


def test_wrapper_old_viewer_rejects_block_diagram_with_clear_error(tmp_path: Path):
    """An old renderer's unknown-option failure is re-raised, naming the release to
    upgrade to."""
    from rtl_buddy.errors import FatalRtlBuddyError
    from rtl_buddy.tools.hier_rtl_buddy_view import VIEW_BLOCK_DIAGRAM_MIN_VERSION

    script = _make_old_view(tmp_path, version="0.7.1")
    view = RtlBuddyView(
        name="t",
        model_cfg=_example_model(tmp_path),
        suite_dir=str(tmp_path),
        format="dot",
        block_diagram=True,
        executable=str(script),
    )
    with pytest.raises(FatalRtlBuddyError) as exc:
        view.run()
    message = str(exc.value)
    assert "--block-diagram" in message
    assert VIEW_BLOCK_DIAGRAM_MIN_VERSION in message
    # The probe names the installed version.
    assert "0.7.1" in message
    # The raw renderer output is still cited.
    assert "hier.log" in message


def test_wrapper_old_viewer_error_survives_a_failed_version_probe(tmp_path: Path):
    """A viewer too old to answer `--version` is still diagnosed; the probe is not a
    gate."""
    from rtl_buddy.errors import FatalRtlBuddyError
    from rtl_buddy.tools.hier_rtl_buddy_view import VIEW_BLOCK_DIAGRAM_MIN_VERSION

    script = _make_old_view(tmp_path, version=None)
    view = RtlBuddyView(
        name="t",
        model_cfg=_example_model(tmp_path),
        suite_dir=str(tmp_path),
        format="dot",
        block_diagram=True,
        executable=str(script),
    )
    with pytest.raises(FatalRtlBuddyError) as exc:
        view.run()
    assert VIEW_BLOCK_DIAGRAM_MIN_VERSION in str(exc.value)
    assert "predates it" in str(exc.value)


def test_wrapper_block_diagram_does_not_hijack_unrelated_failures(tmp_path: Path):
    """An ordinary renderer failure under --block-diagram propagates as an exit code;
    only the unknown-option signature is re-raised."""
    script, _ = _make_fake_view(tmp_path, exit_code=1)
    view = RtlBuddyView(
        name="t",
        model_cfg=_example_model(tmp_path),
        suite_dir=str(tmp_path),
        format="dot",
        block_diagram=True,
        executable=str(script),
    )
    assert view.run() == 1


def test_wrapper_block_diagram_does_not_claim_another_flags_rejection(tmp_path: Path):
    """An unknown-option failure naming another flag is not a --block-diagram version problem.

    hier.log opens with the echoed command line, which repeats every flag passed, so
    the echo is subtracted before matching.
    """
    script = _make_old_view(tmp_path, option="--clock-legend")
    view = RtlBuddyView(
        name="t",
        model_cfg=_example_model(tmp_path),
        suite_dir=str(tmp_path),
        format="dot",
        clock_legend=True,
        block_diagram=True,
        executable=str(script),
    )
    # Propagates as an exit code with no misattributed version error.
    assert view.run() == 2


def test_wrapper_forwards_axi_perf_annotations_as_overlay(tmp_path: Path):
    """--axi-perf-from is forwarded as ``--overlay axi-perf=PATH``."""
    src = tmp_path / "src" / "example.sv"
    src.parent.mkdir()
    src.write_text("module example; endmodule\n")
    model = ModelConfig(
        name="example",
        filelist=[str(src)],
        path=str(tmp_path / "models.yaml"),
    )
    axi_perf = tmp_path / "axi-perf.json"
    axi_perf.write_text("{}")
    script, record = _make_fake_view(tmp_path)

    view = RtlBuddyView(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        axi_perf_annotations=str(axi_perf),
        executable=str(script),
    )
    assert view.run() == 0
    argv = json.loads(record.read_text())
    assert "--overlay" in argv
    assert f"axi-perf={axi_perf}" in argv


def test_wrapper_rejects_missing_axi_perf_annotations(tmp_path: Path):
    """A missing axi-perf file is rejected before the viewer is spawned."""
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
        axi_perf_annotations=str(tmp_path / "missing.json"),
        executable=str(script),
    )
    with pytest.raises(FatalRtlBuddyError) as exc:
        view.run()
    assert "axi-perf" in str(exc.value)


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
    """A nonexistent absolute tool path gives a friendly error, not a FileNotFoundError
    traceback."""
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
    """A bare command name that does not resolve through PATH gets the same error via
    ``shutil.which``."""
    from rtl_buddy.errors import FatalRtlBuddyError

    src = tmp_path / "src" / "example.sv"
    src.parent.mkdir()
    src.write_text("module example; endmodule\n")
    model = ModelConfig(
        name="example",
        filelist=[str(src)],
        path=str(tmp_path / "models.yaml"),
    )
    # An empty PATH makes the lookup fail deterministically.
    monkeypatch.setenv("PATH", "")
    view = RtlBuddyView(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable="totally-fake-binary-xyz",
    )
    with pytest.raises(FatalRtlBuddyError, match="not found on PATH"):
        view.run()


def test_wrapper_resolves_sibling_of_interpreter(tmp_path: Path, monkeypatch):
    """A bare name absent from PATH resolves when the binary sits next to
    ``sys.executable``."""
    import sys as _sys

    bindir = tmp_path / "venvbin"
    bindir.mkdir()
    fake = bindir / "totally-fake-binary-xyz"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(_sys, "executable", str(bindir / "python"))
    monkeypatch.setenv("PATH", "")

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
        executable="totally-fake-binary-xyz",
    )
    # Any exception other than the "not found" FatalRtlBuddyError shows that run() got
    # past resolution.
    try:
        view.run()
    except Exception as exc:  # noqa: BLE001
        assert "not found" not in str(exc)
    assert view.executable == str(fake)


# rb hier command (integration through Typer)


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


def test_rb_hier_block_diagram_flag_reaches_the_renderer(minimal_project: Path):
    """`rb hier <model> --format dot --block-diagram` reaches the renderer with the
    flag."""
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
            "dot",
            "--block-diagram",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code == 0, result.output
    argv = json.loads(record.read_text())
    assert argv[argv.index("--format") + 1] == "dot"
    assert "--block-diagram" in argv


def test_rb_hier_without_block_diagram_omits_it(minimal_project: Path):
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
            "dot",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "--block-diagram" not in json.loads(record.read_text())


def test_rb_hier_block_diagram_on_old_viewer_reports_the_version(
    minimal_project: Path,
):
    """The old-tool path exits non-zero with the actionable message, not a traceback."""
    from rtl_buddy.tools.hier_rtl_buddy_view import VIEW_BLOCK_DIAGRAM_MIN_VERSION

    script = _make_old_view(minimal_project, version="0.7.1")
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "hier",
            "example",
            "-c",
            "models.yaml",
            "--format",
            "dot",
            "--block-diagram",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code != 0
    rendered = result.output + str(result.exception or "")
    assert VIEW_BLOCK_DIAGRAM_MIN_VERSION in rendered
    assert "Traceback" not in result.output


def test_rb_hier_view_tb_forwards_block_diagram(minimal_project: Path):
    """The TB-rooted branch also forwards --block-diagram."""
    script, record = _make_fake_view(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "hier",
            "basic",
            "--view",
            "tb",
            "--test-config",
            "tests.yaml",
            "--format",
            "dot",
            "--block-diagram",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "--block-diagram" in json.loads(record.read_text())


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


# --view tb mode


def test_wrapper_emits_tb_top_and_caches_under_tb_subdir(tmp_path: Path):
    """With ``test_cfg`` the wrapper forwards ``--tb-top <tb.toplevel>`` beside ``--top
    <model>`` and caches the filelist at ``artefacts/hier/<model>/tb/<tb>/hier.f``,
    keyed by (model, tb)."""
    from rtl_buddy.config.test import TestbenchConfig, TestConfig

    src = tmp_path / "src" / "example.sv"
    src.parent.mkdir()
    src.write_text("module example; endmodule\n")
    model = ModelConfig(
        name="example",
        filelist=[str(src)],
        path=str(tmp_path / "models.yaml"),
    )
    tb_src = tmp_path / "src" / "tb.sv"
    tb_src.write_text("module tb_top; example u_dut(); endmodule\n")
    tb = TestbenchConfig(
        name="tb_basic",
        filelist=[str(tb_src)],
        toplevel="tb_top",
    )
    test_cfg = TestConfig(
        name="basic",
        desc="",
        model=model,
        _reglvl=0,
        pa=None,
        pd=None,
        uvm=None,
        preproc_path=None,
        postproc_path=None,
        sweep_path=None,
        tb=tb,
        timeout=None,
    )
    script, record = _make_fake_view(tmp_path)

    view = RtlBuddyView(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        format="json",
        executable=str(script),
        test_cfg=test_cfg,
    )
    assert view.run() == 0

    argv = json.loads(record.read_text())
    # --top is the DUT model; --tb-top is the TB toplevel.
    assert argv[argv.index("--top") + 1] == "example"
    assert argv[argv.index("--tb-top") + 1] == "tb_top"
    # Filelist artefact landed under artefacts/hier/<model>/tb/<tb>/.
    expected_fl = (
        tmp_path / "artefacts" / "hier" / "example" / "tb" / "tb_basic" / "hier.f"
    )
    assert expected_fl.is_file()
    # --filelist points at that file.
    assert argv[argv.index("--filelist") + 1] == str(expected_fl)


def test_wrapper_drops_non_source_tb_filelist_entries(tmp_path: Path):
    """``+incdir+``, ``-y``, ``-v`` and ``+libext+`` entries in the TB filelist are
    dropped from hier.f, since the viewer would open them as source files."""
    from rtl_buddy.config.test import TestbenchConfig, TestConfig
    from rtl_buddy.tools.hier_rtl_buddy_view import _is_non_source_filelist_line

    # Unit-test the helper directly.
    assert _is_non_source_filelist_line("+incdir+../../common")
    assert _is_non_source_filelist_line("+libext+.sv+.v")
    assert _is_non_source_filelist_line("-y rtl/lib")
    assert _is_non_source_filelist_line("-v rtl/some.sv")
    assert not _is_non_source_filelist_line("tb_top.sv")
    assert not _is_non_source_filelist_line("rtl/example.sv")
    # Verilator ``*.vlt`` files are not HDL and Verible fails on them, so they are
    # dropped before the merge.
    assert _is_non_source_filelist_line("../../design/pp_axi.vlt")
    assert _is_non_source_filelist_line("waivers.vlt")
    assert not _is_non_source_filelist_line("axi_2x2.sv")
    # Leading whitespace is tolerated.
    assert _is_non_source_filelist_line("  +incdir+.")

    # Wrapper integration: mixed entries produce a hier.f with only source paths.
    src = tmp_path / "src" / "example.sv"
    src.parent.mkdir()
    src.write_text("module example; endmodule\n")
    incdir = tmp_path / "incdir"
    incdir.mkdir()
    (incdir / "shared.svh").write_text("// header\n")
    model = ModelConfig(
        name="example",
        filelist=[str(src)],
        path=str(tmp_path / "models.yaml"),
    )
    tb_src = tmp_path / "src" / "tb.sv"
    tb_src.write_text("module tb_top; example u_dut(); endmodule\n")
    tb = TestbenchConfig(
        name="tb_basic",
        filelist=[f"+incdir+{incdir}", str(tb_src)],
        toplevel="tb_top",
    )
    test_cfg = TestConfig(
        name="basic",
        desc="",
        model=model,
        _reglvl=0,
        pa=None,
        pd=None,
        uvm=None,
        preproc_path=None,
        postproc_path=None,
        sweep_path=None,
        tb=tb,
        timeout=None,
    )
    script, _ = _make_fake_view(tmp_path)
    view = RtlBuddyView(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        format="json",
        executable=str(script),
        test_cfg=test_cfg,
    )
    assert view.run() == 0
    fl = (
        tmp_path / "artefacts" / "hier" / "example" / "tb" / "tb_basic" / "hier.f"
    ).read_text()
    # +incdir+ entries are filtered before the merge.
    assert str(incdir) not in fl
    assert "+incdir+" not in fl
    # The TB source file survives the filter.
    assert "tb.sv" in fl


def test_wrapper_drops_model_filelist_defines_from_hier_f(tmp_path: Path):
    """A `+define+` in the model's own filelist does not reach hier.f.

    The write step skips defines when `strip` is set, since `rb hier` hands the
    file to rtl-buddy-view, which opens every line as a source. The FPV consumers
    that read the file back keep their defines.
    """
    src = tmp_path / "src" / "example.sv"
    src.parent.mkdir()
    src.write_text("module example; endmodule\n")
    model = ModelConfig(
        name="example",
        filelist=["+define+SYNTHESIS", "+define+WIDTH=8", str(src)],
        path=str(tmp_path / "models.yaml"),
    )
    script, _ = _make_fake_view(tmp_path)
    view = RtlBuddyView(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        format="json",
        executable=str(script),
    )
    assert view.run() == 0

    fl = (tmp_path / "artefacts" / "hier" / "example" / "hier.f").read_text()
    body = [ln for ln in fl.splitlines() if ln and not ln.startswith("//")]
    assert "+define+" not in fl
    # Not as a bare `SYNTHESIS` line the renderer would try to open.
    assert "SYNTHESIS" not in fl
    assert "WIDTH=8" not in fl
    assert [ln for ln in body if ln.endswith("example.sv")]
    assert all(ln.endswith(".sv") for ln in body), body


def test_wrapper_dut_only_does_not_emit_tb_top(tmp_path: Path):
    """With ``test_cfg`` None the renderer gets no ``--tb-top``."""
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
        executable=str(script),
    )
    assert view.run() == 0
    argv = json.loads(record.read_text())
    assert "--tb-top" not in argv


def test_wrapper_tb_top_defaults_to_tb_name_when_toplevel_unset(tmp_path: Path):
    """``--tb-top`` falls back to the testbench config name when ``toplevel`` is unset,
    as for a plain SystemVerilog testbench."""
    from rtl_buddy.config.test import TestbenchConfig, TestConfig

    src = tmp_path / "src" / "example.sv"
    src.parent.mkdir()
    src.write_text("module example; endmodule\n")
    model = ModelConfig(
        name="example",
        filelist=[str(src)],
        path=str(tmp_path / "models.yaml"),
    )
    tb_src = tmp_path / "src" / "tb.sv"
    tb_src.write_text("module tb_axi_2x2; example u_dut(); endmodule\n")
    tb = TestbenchConfig(
        name="tb_axi_2x2",
        filelist=[str(tb_src)],
        toplevel=None,  # plain SV testbench
    )
    test_cfg = TestConfig(
        name="basic",
        desc="",
        model=model,
        _reglvl=0,
        pa=None,
        pd=None,
        uvm=None,
        preproc_path=None,
        postproc_path=None,
        sweep_path=None,
        tb=tb,
        timeout=None,
    )
    script, record = _make_fake_view(tmp_path)
    view = RtlBuddyView(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        format="json",
        executable=str(script),
        test_cfg=test_cfg,
    )
    assert view.run() == 0
    argv = json.loads(record.read_text())
    # Falls back to the testbench config name.
    assert argv[argv.index("--tb-top") + 1] == "tb_axi_2x2"


def test_wrapper_resolves_tb_filelist_against_test_suite_dir(tmp_path: Path):
    """The TB filelist's relative entries resolve against ``test_suite_dir``, not the
    cwd, or the merge raises ``FilelistError``."""
    from rtl_buddy.config.test import TestbenchConfig, TestConfig
    from rtl_buddy.errors import FilelistError

    suite = tmp_path / "verif" / "demo"
    suite.mkdir(parents=True)
    tb_src = suite / "tb.sv"
    tb_src.write_text("module tb_top; example u_dut(); endmodule\n")
    src = tmp_path / "design" / "example.sv"
    src.parent.mkdir()
    src.write_text("module example; endmodule\n")
    model = ModelConfig(
        name="example",
        filelist=[str(src)],
        path=str(tmp_path / "design" / "models.yaml"),
    )

    def _make_test_cfg() -> TestConfig:
        tb = TestbenchConfig(
            name="tb_basic",
            filelist=["tb.sv"],  # relative to the suite dir
            toplevel="tb_top",
        )
        return TestConfig(
            name="basic",
            desc="",
            model=model,
            _reglvl=0,
            pa=None,
            pd=None,
            uvm=None,
            preproc_path=None,
            postproc_path=None,
            sweep_path=None,
            tb=tb,
            timeout=None,
        )

    script, _ = _make_fake_view(tmp_path)

    # Correct suite dir: the relative TB entry resolves.
    view = RtlBuddyView(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),  # artefact root
        format="json",
        executable=str(script),
        test_cfg=_make_test_cfg(),
        test_suite_dir=str(suite),
    )
    assert view.run() == 0
    fl = (
        tmp_path / "artefacts" / "hier" / "example" / "tb" / "tb_basic" / "hier.f"
    ).read_text()
    assert "tb.sv" in fl

    # Wrong suite dir: the entry is not found and ``FilelistError`` surfaces.
    bad = RtlBuddyView(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        format="json",
        executable=str(script),
        test_cfg=_make_test_cfg(),
        test_suite_dir=str(tmp_path),  # tb.sv does not live here
    )
    with pytest.raises(FilelistError):
        bad.run()


def test_rb_hier_view_tb_resolves_test_and_invokes_renderer(minimal_project: Path):
    script, record = _make_fake_view(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "hier",
            "basic",
            "--view",
            "tb",
            "--test-config",
            "tests.yaml",
            "--format",
            "json",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code == 0, result.output
    argv = json.loads(record.read_text())
    # ``basic`` pins model=example (DUT) and tb=tb_basic.
    assert argv[argv.index("--top") + 1] == "example"
    assert argv[argv.index("--tb-top") + 1] == "tb_basic"
    # Filelist landed under the (model, tb) cache path.
    assert (
        minimal_project
        / "artefacts"
        / "hier"
        / "example"
        / "tb"
        / "tb_basic"
        / "hier.f"
    ).is_file()


def test_rb_hier_view_must_be_dut_or_tb(minimal_project: Path):
    """An unknown --view value raises FatalRtlBuddyError before the renderer runs."""
    script, _ = _make_fake_view(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "hier",
            "basic",
            "--view",
            "wave",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code != 0


# RtlBuddyViewQuery wrapper (unit)


def _query_model(tmp_path: Path) -> ModelConfig:
    src = tmp_path / "src" / "example.sv"
    src.parent.mkdir(exist_ok=True)
    src.write_text("module example; endmodule\n")
    return ModelConfig(
        name="example",
        filelist=[str(src)],
        path=str(tmp_path / "models.yaml"),
    )


def test_query_wrapper_builds_expected_argv(tmp_path: Path):
    """The argv is ``query <verb> <arg> --top <model> --filelist <hier.f>``."""
    from rtl_buddy.tools.hier_rtl_buddy_view import RtlBuddyViewQuery

    script, record = _make_fake_view(tmp_path)
    view = RtlBuddyViewQuery(
        name="t",
        model_cfg=_query_model(tmp_path),
        suite_dir=str(tmp_path),
        verb="find-module",
        arg="some_module",
        executable=str(script),
    )
    assert view.run() == 0

    argv = json.loads(record.read_text())
    assert argv[:3] == ["query", "find-module", "some_module"]
    assert argv[argv.index("--top") + 1] == "example"
    fl = Path(argv[argv.index("--filelist") + 1])
    assert fl.name == "hier.f" and fl.is_file()
    # Shares `rb hier`'s filelist artefact; its own log sits alongside.
    assert fl.parent == tmp_path / "artefacts" / "hier" / "example"
    assert (fl.parent / "query.log").read_text().startswith("$ ")


def test_query_wrapper_forwards_verb_specific_flags(tmp_path: Path):
    from rtl_buddy.tools.hier_rtl_buddy_view import RtlBuddyViewQuery

    script, record = _make_fake_view(tmp_path)
    model = _query_model(tmp_path)

    view = RtlBuddyViewQuery(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        verb="source-snippet",
        arg="example.u_ff",
        frontend="slang",
        context=0,
        line_numbers=False,
        executable=str(script),
    )
    assert view.run() == 0
    argv = json.loads(record.read_text())
    assert argv[argv.index("--frontend") + 1] == "slang"
    assert argv[argv.index("--context") + 1] == "0"
    assert "--no-line-numbers" in argv

    view = RtlBuddyViewQuery(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        verb="subtree",
        arg="example.u_ff",
        subtree_format="tree",
        executable=str(script),
    )
    assert view.run() == 0
    argv = json.loads(record.read_text())
    assert argv[argv.index("--format") + 1] == "tree"


def test_query_wrapper_gates_flags_by_verb(tmp_path: Path):
    """Verb-specific flags are forwarded only to verbs that accept them."""
    from rtl_buddy.tools.hier_rtl_buddy_view import RtlBuddyViewQuery

    script, record = _make_fake_view(tmp_path)
    view = RtlBuddyViewQuery(
        name="t",
        model_cfg=_query_model(tmp_path),
        suite_dir=str(tmp_path),
        verb="find-module",
        arg="some_module",
        subtree_format="tree",
        context=5,
        line_numbers=False,
        executable=str(script),
    )
    assert view.run() == 0
    argv = json.loads(record.read_text())
    for flag in ("--format", "--context", "--no-line-numbers"):
        assert flag not in argv


def test_query_wrapper_rejects_unknown_verb(tmp_path: Path):
    from rtl_buddy.errors import FatalRtlBuddyError
    from rtl_buddy.tools.hier_rtl_buddy_view import RtlBuddyViewQuery

    script, _ = _make_fake_view(tmp_path)
    with pytest.raises(FatalRtlBuddyError, match="unknown verb 'walk'"):
        RtlBuddyViewQuery(
            name="t",
            model_cfg=_query_model(tmp_path),
            suite_dir=str(tmp_path),
            verb="walk",
            arg="x",
            executable=str(script),
        )


def test_query_wrapper_propagates_exit_code(tmp_path: Path):
    """A lookup miss exits 1 in the viewer and `rb hier-query` propagates it."""
    from rtl_buddy.tools.hier_rtl_buddy_view import RtlBuddyViewQuery

    script, _ = _make_fake_view(tmp_path, exit_code=1)
    view = RtlBuddyViewQuery(
        name="t",
        model_cfg=_query_model(tmp_path),
        suite_dir=str(tmp_path),
        verb="subtree",
        arg="example.nope",
        executable=str(script),
    )
    assert view.run() == 1


# rb hier-query command (integration through Typer)


def test_rb_hier_query_invokes_stubbed_viewer(minimal_project: Path):
    script, record = _make_fake_view(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "hier-query",
            "example",
            "instances-of",
            "some_module",
            "-c",
            "models.yaml",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code == 0, result.output
    argv = json.loads(record.read_text())
    assert argv[:3] == ["query", "instances-of", "some_module"]
    assert argv[argv.index("--top") + 1] == "example"
    assert (minimal_project / "artefacts" / "hier" / "example" / "hier.f").is_file()


def test_rb_hier_query_forwards_snippet_options(minimal_project: Path):
    script, record = _make_fake_view(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "hier-query",
            "example",
            "source-snippet",
            "example.u_ff",
            "-c",
            "models.yaml",
            "--context",
            "4",
            "--no-line-numbers",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code == 0, result.output
    argv = json.loads(record.read_text())
    assert argv[argv.index("--context") + 1] == "4"
    assert "--no-line-numbers" in argv


def test_rb_hier_query_unknown_verb_exits_nonzero(minimal_project: Path):
    script, _ = _make_fake_view(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "hier-query",
            "example",
            "walk",
            "x",
            "-c",
            "models.yaml",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code != 0


def test_rb_hier_query_viewer_exit_propagates(minimal_project: Path):
    script, _ = _make_fake_view(minimal_project, exit_code=1)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "hier-query",
            "example",
            "find-module",
            "nope",
            "-c",
            "models.yaml",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code == 1
