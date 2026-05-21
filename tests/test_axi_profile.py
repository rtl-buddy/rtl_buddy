"""Tests for the ``rb axi-profile`` command + ``RtlBuddyAxiProfile`` wrapper.

Same fake-binary pattern as ``tests/test_hier.py``: the real
``axi-profiler`` is not on PATH in CI, so we stub it with a tiny
shell script that records its argv. This pins the CLI shape we
promise to the downstream profiler (``discover --filelist ... --top
... --output ...``).
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from rtl_buddy.config.model import ModelConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools.axi_profile_rtl_buddy import RtlBuddyAxiProfile


def _make_fake_profiler(tmp_path: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    """Drop a fake ``axi-profiler`` that records argv to a JSON sidecar."""
    record = tmp_path / "axi-profiler-argv.json"
    script = tmp_path / "axi-profiler"
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


def _make_model(tmp_path: Path) -> ModelConfig:
    src = tmp_path / "src" / "soc.sv"
    src.parent.mkdir()
    src.write_text("module soc; endmodule\n")
    return ModelConfig(
        name="soc",
        filelist=[str(src)],
        path=str(tmp_path / "models.yaml"),
    )


def test_wrapper_builds_expected_argv(tmp_path: Path) -> None:
    model = _make_model(tmp_path)
    script, record = _make_fake_profiler(tmp_path)

    profiler = RtlBuddyAxiProfile(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    assert profiler.run() == 0

    argv = json.loads(record.read_text())
    # First positional is the `discover` subcommand.
    assert argv[0] == "discover"
    # Required options.
    assert "--filelist" in argv
    fl_idx = argv.index("--filelist") + 1
    assert argv[fl_idx].endswith("axi.f")
    assert Path(argv[fl_idx]).is_file()
    assert "--top" in argv and argv[argv.index("--top") + 1] == "soc"
    assert "--output" in argv
    out_idx = argv.index("--output") + 1
    assert argv[out_idx].endswith("axi-bundles.yaml")


def test_wrapper_forwards_amend_flag(tmp_path: Path) -> None:
    model = _make_model(tmp_path)
    script, record = _make_fake_profiler(tmp_path)

    amend_path = tmp_path / "existing.yaml"
    amend_path.write_text("schema_version: '1.0'\nbundles: []\n")

    profiler = RtlBuddyAxiProfile(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        amend=str(amend_path),
        executable=str(script),
    )
    assert profiler.run() == 0

    argv = json.loads(record.read_text())
    assert "--amend" in argv
    assert argv[argv.index("--amend") + 1] == str(amend_path)


def test_wrapper_default_output_path_is_artefacts(tmp_path: Path) -> None:
    """Default output lands under artefacts/axi/<model>/axi-bundles.yaml."""
    model = _make_model(tmp_path)
    script, record = _make_fake_profiler(tmp_path)

    profiler = RtlBuddyAxiProfile(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    assert profiler.run() == 0
    argv = json.loads(record.read_text())
    out = argv[argv.index("--output") + 1]
    assert "artefacts/axi/soc/axi-bundles.yaml" in out


def test_wrapper_explicit_output_overrides_default(tmp_path: Path) -> None:
    model = _make_model(tmp_path)
    script, record = _make_fake_profiler(tmp_path)
    custom_out = tmp_path / "my-axi.yaml"

    profiler = RtlBuddyAxiProfile(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        output=str(custom_out),
        executable=str(script),
    )
    assert profiler.run() == 0
    argv = json.loads(record.read_text())
    assert argv[argv.index("--output") + 1] == str(custom_out)


def test_wrapper_propagates_nonzero_exit(tmp_path: Path) -> None:
    model = _make_model(tmp_path)
    script, _record = _make_fake_profiler(tmp_path, exit_code=3)

    profiler = RtlBuddyAxiProfile(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    assert profiler.run() == 3


def test_wrapper_errors_when_executable_missing(tmp_path: Path) -> None:
    model = _make_model(tmp_path)
    profiler = RtlBuddyAxiProfile(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable="this-binary-definitely-does-not-exist",
    )
    with pytest.raises(FatalRtlBuddyError) as info:
        profiler.run()
    assert "axi-profiler" in str(info.value)


def test_wrapper_errors_when_explicit_path_not_executable(tmp_path: Path) -> None:
    """A path-style executable that doesn't exist gives a clean error."""
    bogus = tmp_path / "bogus" / "axi-profiler"
    model = _make_model(tmp_path)
    profiler = RtlBuddyAxiProfile(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(bogus),
    )
    with pytest.raises(FatalRtlBuddyError):
        profiler.run()


def test_wrapper_forwards_tb_prefix_flag(tmp_path: Path) -> None:
    """When tb_prefix is set, it should be forwarded to axi-profiler
    as --tb-prefix <value>."""
    model = _make_model(tmp_path)
    script, record = _make_fake_profiler(tmp_path)

    profiler = RtlBuddyAxiProfile(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        tb_prefix="tb_soc.dut",
        executable=str(script),
    )
    assert profiler.run() == 0

    argv = json.loads(record.read_text())
    assert "--tb-prefix" in argv
    assert argv[argv.index("--tb-prefix") + 1] == "tb_soc.dut"


def test_wrapper_omits_tb_prefix_when_unset(tmp_path: Path) -> None:
    """No tb_prefix → no --tb-prefix flag emitted."""
    model = _make_model(tmp_path)
    script, record = _make_fake_profiler(tmp_path)

    profiler = RtlBuddyAxiProfile(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    assert profiler.run() == 0
    argv = json.loads(record.read_text())
    assert "--tb-prefix" not in argv


def test_wrapper_writes_log_with_command_line(tmp_path: Path) -> None:
    model = _make_model(tmp_path)
    script, _record = _make_fake_profiler(tmp_path)

    profiler = RtlBuddyAxiProfile(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    profiler.run()
    log_text = Path(profiler._log_path()).read_text()
    assert log_text.startswith("$ ")
    assert "discover" in log_text
