"""Tests for the ``rb axi-profile`` subcommand group + wrappers.

Same fake-binary pattern as ``tests/test_hier.py``: the real
``axi-profiler`` isn't on PATH in CI, so we stub it with a tiny
shell script that records its argv. This pins the CLI shapes we
promise the downstream:

* ``axi-profiler discover --filelist ... --top ... --output ...``
* ``axi-profiler run --filelist ... --top ... --input ... --manifest ... --output ... [--tb-prefix ...]``
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.config.model import ModelConfig
from rtl_buddy.config.suite import SuiteConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.tools.axi_profile_rtl_buddy import (
    RtlBuddyAxiProfileDiscover,
    RtlBuddyAxiProfileGenMonitor,
    RtlBuddyAxiProfileRun,
)


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


def _make_model(
    tmp_path: Path,
    *,
    axi_bundles: str | None = None,
    axi_monitor_out: str | None = None,
) -> ModelConfig:
    src = tmp_path / "src" / "soc.sv"
    src.parent.mkdir(exist_ok=True)
    src.write_text("module soc; endmodule\n")
    return ModelConfig(
        name="soc",
        filelist=[str(src)],
        axi_bundles=axi_bundles,
        axi_monitor_out=axi_monitor_out,
        path=str(tmp_path / "models.yaml"),
    )


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_axi_profile")


# ---------------------------------------------------------------------------
# RtlBuddyAxiProfileDiscover (unit)
# ---------------------------------------------------------------------------


def test_discover_wrapper_builds_expected_argv(tmp_path: Path) -> None:
    model = _make_model(tmp_path)
    script, record = _make_fake_profiler(tmp_path)

    profiler = RtlBuddyAxiProfileDiscover(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    assert profiler.run() == 0

    argv = json.loads(record.read_text())
    assert argv[0] == "discover"
    fl_idx = argv.index("--filelist") + 1
    assert argv[fl_idx].endswith("axi.f")
    assert Path(argv[fl_idx]).is_file()
    assert argv[argv.index("--top") + 1] == "soc"
    assert argv[argv.index("--output") + 1].endswith("axi-bundles.yaml")


def test_discover_default_output_falls_back_to_artefacts(tmp_path: Path) -> None:
    """Without `axi_bundles:` set the default output lands under artefacts/."""
    model = _make_model(tmp_path)
    script, record = _make_fake_profiler(tmp_path)

    profiler = RtlBuddyAxiProfileDiscover(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    assert profiler.run() == 0
    argv = json.loads(record.read_text())
    out = argv[argv.index("--output") + 1]
    assert "artefacts/axi/soc/axi-bundles.yaml" in out


def test_discover_default_output_uses_model_axi_bundles_when_set(
    tmp_path: Path,
) -> None:
    """With `axi_bundles:` set, discover writes there by default."""
    model = _make_model(tmp_path, axi_bundles="src/axi-bundles.yaml")
    script, record = _make_fake_profiler(tmp_path)

    profiler = RtlBuddyAxiProfileDiscover(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    assert profiler.run() == 0
    argv = json.loads(record.read_text())
    expected = str(tmp_path / "src" / "axi-bundles.yaml")
    assert argv[argv.index("--output") + 1] == expected


def test_discover_explicit_output_overrides_default(tmp_path: Path) -> None:
    model = _make_model(tmp_path, axi_bundles="src/axi-bundles.yaml")
    script, record = _make_fake_profiler(tmp_path)
    custom_out = tmp_path / "elsewhere" / "my-axi.yaml"
    custom_out.parent.mkdir()

    profiler = RtlBuddyAxiProfileDiscover(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        output=str(custom_out),
        executable=str(script),
    )
    assert profiler.run() == 0
    argv = json.loads(record.read_text())
    assert argv[argv.index("--output") + 1] == str(custom_out)


def test_discover_forwards_amend_flag(tmp_path: Path) -> None:
    model = _make_model(tmp_path)
    script, record = _make_fake_profiler(tmp_path)
    amend_path = tmp_path / "existing.yaml"
    amend_path.write_text("schema_version: '1.0'\nbundles: []\n")

    profiler = RtlBuddyAxiProfileDiscover(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        amend=str(amend_path),
        executable=str(script),
    )
    assert profiler.run() == 0
    argv = json.loads(record.read_text())
    assert argv[argv.index("--amend") + 1] == str(amend_path)


def test_discover_propagates_nonzero_exit(tmp_path: Path) -> None:
    model = _make_model(tmp_path)
    script, _ = _make_fake_profiler(tmp_path, exit_code=3)

    profiler = RtlBuddyAxiProfileDiscover(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    assert profiler.run() == 3


def test_discover_errors_when_executable_missing(tmp_path: Path) -> None:
    model = _make_model(tmp_path)
    profiler = RtlBuddyAxiProfileDiscover(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable="this-binary-definitely-does-not-exist",
    )
    with pytest.raises(FatalRtlBuddyError) as info:
        profiler.run()
    assert "axi-profiler" in str(info.value)


# ---------------------------------------------------------------------------
# RtlBuddyAxiProfileRun (unit)
# ---------------------------------------------------------------------------


def _write_run_fixture(
    tmp_path: Path, *, axi_bundles_present: bool = True, fst_present: bool = True
) -> tuple[Path, Path]:
    """Build a self-contained suite_dir with tests.yaml + models.yaml.

    Returns ``(suite_dir, tests_yaml_path)``. The fixture has one test
    ``basic`` over a testbench ``tb_basic`` over a model ``soc``. Toggles
    let individual tests exercise the missing-manifest / missing-FST
    branches.
    """
    suite_dir = tmp_path / "verif" / "soc_top"
    suite_dir.mkdir(parents=True)
    src = suite_dir / "src" / "soc.sv"
    src.parent.mkdir()
    src.write_text("module soc; endmodule\n")

    models_yaml = suite_dir / "models.yaml"
    bundles_field = ""
    if axi_bundles_present:
        bundles_field = "    axi_bundles: src/axi-bundles.yaml\n"
        (suite_dir / "src" / "axi-bundles.yaml").write_text(
            "schema_version: '1.0'\nbundles: []\n"
        )
    models_yaml.write_text(
        "rtl-buddy-filetype: model_config\n"
        "models:\n"
        "  - name: soc\n"
        "    filelist:\n"
        "      - src/soc.sv\n" + bundles_field
    )

    tests_yaml = suite_dir / "tests.yaml"
    tests_yaml.write_text(
        "rtl-buddy-filetype: test_config\n"
        "testbenches:\n"
        "  - name: tb_basic\n"
        "    filelist:\n"
        "      - src/soc.sv\n"
        "tests:\n"
        "  - name: basic\n"
        "    desc: smoke\n"
        "    model: soc\n"
        "    model_path: models.yaml\n"
        "    reglvl: 0\n"
        "    testbench: tb_basic\n"
        "    plusargs:\n"
        "    plusdefines:\n"
        "    uvm:\n"
        "    preproc:\n"
        "    postproc:\n"
        "    sweep:\n"
        "    sim_timeout:\n"
    )

    if fst_present:
        fst_dir = suite_dir / "artefacts" / "basic"
        fst_dir.mkdir(parents=True)
        (fst_dir / "dump.fst").write_text("not a real FST, but a real file\n")

    return suite_dir, tests_yaml


def test_run_wrapper_builds_expected_argv(tmp_path: Path) -> None:
    suite_dir, tests_yaml = _write_run_fixture(tmp_path)
    script, record = _make_fake_profiler(tmp_path)

    suite_cfg = SuiteConfig(str(tests_yaml))
    test_cfg = suite_cfg.get_tests("basic")[0]

    profiler = RtlBuddyAxiProfileRun(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        executable=str(script),
    )
    assert profiler.run() == 0

    argv = json.loads(record.read_text())
    assert argv[0] == "run"
    assert argv[argv.index("--top") + 1] == "soc"
    assert argv[argv.index("--input") + 1].endswith("artefacts/basic/dump.fst")
    assert argv[argv.index("--manifest") + 1].endswith("src/axi-bundles.yaml")
    assert argv[argv.index("--output") + 1].endswith(
        "artefacts/axi/basic/axi-perf.json"
    )
    # tb_prefix defaults to the testbench name from tests.yaml.
    assert argv[argv.index("--tb-prefix") + 1] == "tb_basic"


def test_run_wrapper_tb_prefix_override_wins(tmp_path: Path) -> None:
    suite_dir, tests_yaml = _write_run_fixture(tmp_path)
    script, record = _make_fake_profiler(tmp_path)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]

    profiler = RtlBuddyAxiProfileRun(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        tb_prefix_override="tb_soc.dut",
        executable=str(script),
    )
    assert profiler.run() == 0
    argv = json.loads(record.read_text())
    assert argv[argv.index("--tb-prefix") + 1] == "tb_soc.dut"


def test_run_wrapper_tb_prefix_empty_override_disables_flag(tmp_path: Path) -> None:
    """Explicit empty string opts out of the --tb-prefix flag."""
    suite_dir, tests_yaml = _write_run_fixture(tmp_path)
    script, record = _make_fake_profiler(tmp_path)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]

    profiler = RtlBuddyAxiProfileRun(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        tb_prefix_override="",
        executable=str(script),
    )
    assert profiler.run() == 0
    argv = json.loads(record.read_text())
    assert "--tb-prefix" not in argv


def test_run_wrapper_output_override(tmp_path: Path) -> None:
    suite_dir, tests_yaml = _write_run_fixture(tmp_path)
    script, record = _make_fake_profiler(tmp_path)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]
    custom_out = suite_dir / "out" / "perf.json"
    custom_out.parent.mkdir()

    profiler = RtlBuddyAxiProfileRun(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        output=str(custom_out),
        executable=str(script),
    )
    assert profiler.run() == 0
    argv = json.loads(record.read_text())
    assert argv[argv.index("--output") + 1] == str(custom_out)


def test_run_wrapper_errors_when_axi_bundles_unset(tmp_path: Path) -> None:
    """Model without `axi_bundles:` field → hint to set it + run discover."""
    suite_dir, tests_yaml = _write_run_fixture(tmp_path, axi_bundles_present=False)
    script, _ = _make_fake_profiler(tmp_path)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]

    profiler = RtlBuddyAxiProfileRun(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        executable=str(script),
    )
    with pytest.raises(FatalRtlBuddyError) as info:
        profiler.run()
    msg = str(info.value)
    assert "axi_bundles" in msg
    assert "rb axi-profile discover soc" in msg


def test_run_wrapper_errors_when_manifest_file_missing(tmp_path: Path) -> None:
    """`axi_bundles:` set but the file doesn't exist → hint to run discover."""
    suite_dir, tests_yaml = _write_run_fixture(tmp_path)
    # Delete the manifest file but leave the field pointing at it.
    (suite_dir / "src" / "axi-bundles.yaml").unlink()
    script, _ = _make_fake_profiler(tmp_path)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]

    profiler = RtlBuddyAxiProfileRun(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        executable=str(script),
    )
    with pytest.raises(FatalRtlBuddyError) as info:
        profiler.run()
    msg = str(info.value)
    assert "manifest not found" in msg
    assert "rb axi-profile discover soc" in msg


def test_run_wrapper_errors_when_fst_missing(tmp_path: Path) -> None:
    """No FST under artefacts/<test>/ → hint to run `rb test <test>` first."""
    suite_dir, tests_yaml = _write_run_fixture(tmp_path, fst_present=False)
    script, _ = _make_fake_profiler(tmp_path)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]

    profiler = RtlBuddyAxiProfileRun(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        executable=str(script),
    )
    with pytest.raises(FatalRtlBuddyError) as info:
        profiler.run()
    msg = str(info.value)
    assert "FST not found" in msg
    assert "rb test basic" in msg


def test_run_wrapper_emits_parquet_at_artefact_default(tmp_path: Path) -> None:
    """Empty-string `emit_txns_parquet` → wrapper picks the artefact-dir
    default that `rb axi-profile notebook` reads (axi-txns.parquet
    next to axi-perf.json)."""
    suite_dir, tests_yaml = _write_run_fixture(tmp_path)
    script, record = _make_fake_profiler(tmp_path)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]

    profiler = RtlBuddyAxiProfileRun(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        emit_txns_parquet="",
        executable=str(script),
    )
    assert profiler.run() == 0
    argv = json.loads(record.read_text())
    parquet_arg = argv[argv.index("--emit-txns-parquet") + 1]
    assert parquet_arg.endswith("artefacts/axi/basic/axi-txns.parquet")


def test_run_wrapper_emits_parquet_at_explicit_path(tmp_path: Path) -> None:
    """Explicit path wins over the default."""
    suite_dir, tests_yaml = _write_run_fixture(tmp_path)
    script, record = _make_fake_profiler(tmp_path)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]
    custom = tmp_path / "elsewhere" / "my-txns.parquet"

    profiler = RtlBuddyAxiProfileRun(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        emit_txns_parquet=str(custom),
        executable=str(script),
    )
    assert profiler.run() == 0
    argv = json.loads(record.read_text())
    assert argv[argv.index("--emit-txns-parquet") + 1] == str(custom)


def test_run_wrapper_omits_parquet_flag_by_default(tmp_path: Path) -> None:
    """Legacy behaviour: no --emit-txns-parquet flag unless asked."""
    suite_dir, tests_yaml = _write_run_fixture(tmp_path)
    script, record = _make_fake_profiler(tmp_path)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]

    profiler = RtlBuddyAxiProfileRun(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        executable=str(script),
    )
    assert profiler.run() == 0
    argv = json.loads(record.read_text())
    assert "--emit-txns-parquet" not in argv


def test_rb_axi_profile_run_emit_parquet_via_cli(minimal_project: Path) -> None:
    """End-to-end: `rb axi-profile run --emit-txns-parquet` plumbs the
    flag through to axi-profiler with the artefact-dir default path."""
    models_yaml = minimal_project / "models.yaml"
    models_yaml.write_text(
        models_yaml.read_text() + "    axi_bundles: src/axi-bundles.yaml\n"
    )
    (minimal_project / "src" / "axi-bundles.yaml").write_text(
        "schema_version: '1.0'\nbundles: []\n"
    )
    fst_dir = minimal_project / "artefacts" / "basic"
    fst_dir.mkdir(parents=True)
    (fst_dir / "dump.fst").write_text("fake fst\n")

    script, record = _make_fake_profiler(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "axi-profile",
            "run",
            "basic",
            "-c",
            "tests.yaml",
            "--emit-txns-parquet",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code == 0, result.output
    argv = json.loads(record.read_text())
    parquet_arg = argv[argv.index("--emit-txns-parquet") + 1]
    assert parquet_arg.endswith("artefacts/axi/basic/axi-txns.parquet")


def test_run_wrapper_propagates_nonzero_exit(tmp_path: Path) -> None:
    suite_dir, tests_yaml = _write_run_fixture(tmp_path)
    script, _ = _make_fake_profiler(tmp_path, exit_code=4)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]

    profiler = RtlBuddyAxiProfileRun(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        executable=str(script),
    )
    assert profiler.run() == 4


# ---------------------------------------------------------------------------
# rb axi-profile (integration through Typer)
# ---------------------------------------------------------------------------


def test_rb_axi_profile_discover_invokes_stubbed_profiler(
    minimal_project: Path,
) -> None:
    script, record = _make_fake_profiler(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "axi-profile",
            "discover",
            "example",
            "-c",
            "models.yaml",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code == 0, result.output
    argv = json.loads(record.read_text())
    assert argv[0] == "discover"
    assert argv[argv.index("--top") + 1] == "example"


def test_rb_axi_profile_run_invokes_stubbed_profiler(minimal_project: Path) -> None:
    """End-to-end: rb axi-profile run <test> through the Typer app.

    The minimal_project fixture's models.yaml lacks `axi_bundles:`, so
    extend it in-place for this test and pre-create both the manifest
    and the FST.
    """
    models_yaml = minimal_project / "models.yaml"
    models_yaml.write_text(
        models_yaml.read_text() + "    axi_bundles: src/axi-bundles.yaml\n"
    )
    (minimal_project / "src" / "axi-bundles.yaml").write_text(
        "schema_version: '1.0'\nbundles: []\n"
    )
    fst_dir = minimal_project / "artefacts" / "basic"
    fst_dir.mkdir(parents=True)
    (fst_dir / "dump.fst").write_text("fake fst\n")

    script, record = _make_fake_profiler(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "axi-profile",
            "run",
            "basic",
            "-c",
            "tests.yaml",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code == 0, result.output
    argv = json.loads(record.read_text())
    assert argv[0] == "run"
    assert argv[argv.index("--input") + 1].endswith("artefacts/basic/dump.fst")
    assert argv[argv.index("--manifest") + 1].endswith("src/axi-bundles.yaml")
    # tb_basic is the testbench name in the fixture's tests.yaml.
    assert argv[argv.index("--tb-prefix") + 1] == "tb_basic"


def test_rb_axi_profile_no_subcommand_shows_help(minimal_project: Path) -> None:
    """`rb axi-profile` with no subcommand must not run anything."""
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["axi-profile"])
    # Typer's no_args_is_help convention exits non-zero and emits help text.
    assert result.exit_code != 0
    assert "discover" in result.output
    assert "run" in result.output
    assert "gen-monitor" in result.output


# ---------------------------------------------------------------------------
# RtlBuddyAxiProfileGenMonitor (unit)
# ---------------------------------------------------------------------------


def _make_gen_monitor_model(
    tmp_path: Path,
    *,
    axi_bundles_present: bool = True,
    axi_monitor_out: str | None = "../verif/soc_top/gen/axi_perf_mon.sv",
    create_manifest_file: bool = True,
) -> ModelConfig:
    """Build a ModelConfig + on-disk manifest for gen-monitor tests."""
    design = tmp_path / "design" / "soc"
    design.mkdir(parents=True)
    src = design / "soc.sv"
    src.write_text("module soc; endmodule\n")

    axi_bundles_field = "src/axi-bundles.yaml" if axi_bundles_present else None
    if axi_bundles_present and create_manifest_file:
        manifest = design / "src" / "axi-bundles.yaml"
        manifest.parent.mkdir(exist_ok=True)
        manifest.write_text("schema_version: '1.0'\nbundles: []\n")

    return ModelConfig(
        name="soc",
        filelist=[str(src)],
        axi_bundles=axi_bundles_field,
        axi_monitor_out=axi_monitor_out,
        path=str(design / "models.yaml"),
    )


def test_gen_monitor_wrapper_builds_expected_argv(tmp_path: Path) -> None:
    model = _make_gen_monitor_model(tmp_path)
    script, record = _make_fake_profiler(tmp_path)

    profiler = RtlBuddyAxiProfileGenMonitor(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    assert profiler.run() == 0

    argv = json.loads(record.read_text())
    # `gen-monitor <manifest> --output <out>` (manifest is positional).
    assert argv[0] == "gen-monitor"
    assert argv[1].endswith("src/axi-bundles.yaml")
    assert argv[argv.index("--output") + 1].endswith(
        "verif/soc_top/gen/axi_perf_mon.sv"
    )
    # Both flags optional; omitted when not set.
    assert "--time-precision" not in argv
    assert "--buffer-cap" not in argv


def test_gen_monitor_wrapper_forwards_optional_flags(tmp_path: Path) -> None:
    model = _make_gen_monitor_model(tmp_path)
    script, record = _make_fake_profiler(tmp_path)

    profiler = RtlBuddyAxiProfileGenMonitor(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        time_precision="100ps",
        buffer_cap=4096,
        executable=str(script),
    )
    assert profiler.run() == 0
    argv = json.loads(record.read_text())
    assert argv[argv.index("--time-precision") + 1] == "100ps"
    assert argv[argv.index("--buffer-cap") + 1] == "4096"


def test_gen_monitor_wrapper_output_override(tmp_path: Path) -> None:
    """--output overrides the `axi_monitor_out:` default."""
    model = _make_gen_monitor_model(tmp_path)
    custom_out = tmp_path / "elsewhere" / "mon.sv"
    custom_out.parent.mkdir()
    script, record = _make_fake_profiler(tmp_path)

    profiler = RtlBuddyAxiProfileGenMonitor(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        output=str(custom_out),
        executable=str(script),
    )
    assert profiler.run() == 0
    argv = json.loads(record.read_text())
    assert argv[argv.index("--output") + 1] == str(custom_out)


def test_gen_monitor_wrapper_errors_when_axi_bundles_unset(tmp_path: Path) -> None:
    model = _make_gen_monitor_model(tmp_path, axi_bundles_present=False)
    script, _ = _make_fake_profiler(tmp_path)

    profiler = RtlBuddyAxiProfileGenMonitor(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    with pytest.raises(FatalRtlBuddyError) as info:
        profiler.run()
    msg = str(info.value)
    assert "axi_bundles" in msg
    assert "rb axi-profile discover soc" in msg


def test_gen_monitor_wrapper_errors_when_manifest_file_missing(tmp_path: Path) -> None:
    model = _make_gen_monitor_model(
        tmp_path, axi_bundles_present=True, create_manifest_file=False
    )
    script, _ = _make_fake_profiler(tmp_path)

    profiler = RtlBuddyAxiProfileGenMonitor(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    with pytest.raises(FatalRtlBuddyError) as info:
        profiler.run()
    msg = str(info.value)
    assert "manifest not found" in msg


def test_gen_monitor_wrapper_errors_when_monitor_out_unset(tmp_path: Path) -> None:
    """Model without `axi_monitor_out:` AND no --output → hint."""
    model = _make_gen_monitor_model(tmp_path, axi_monitor_out=None)
    script, _ = _make_fake_profiler(tmp_path)

    profiler = RtlBuddyAxiProfileGenMonitor(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    with pytest.raises(FatalRtlBuddyError) as info:
        profiler.run()
    msg = str(info.value)
    assert "axi_monitor_out" in msg


def test_gen_monitor_wrapper_creates_parent_dirs(tmp_path: Path) -> None:
    """The output's parent dir is created so first run doesn't fail."""
    model = _make_gen_monitor_model(tmp_path)
    script, _ = _make_fake_profiler(tmp_path)

    profiler = RtlBuddyAxiProfileGenMonitor(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    assert profiler.run() == 0
    # Parent of axi_monitor_out (../verif/soc_top/gen/) should exist.
    out_parent = (tmp_path / "design" / "verif" / "soc_top" / "gen").resolve()
    assert out_parent.is_dir()


def test_gen_monitor_wrapper_propagates_nonzero_exit(tmp_path: Path) -> None:
    model = _make_gen_monitor_model(tmp_path)
    script, _ = _make_fake_profiler(tmp_path, exit_code=2)

    profiler = RtlBuddyAxiProfileGenMonitor(
        name="t",
        model_cfg=model,
        suite_dir=str(tmp_path),
        executable=str(script),
    )
    assert profiler.run() == 2


def test_rb_axi_profile_gen_monitor_invokes_stubbed_profiler(
    minimal_project: Path,
) -> None:
    """End-to-end: rb axi-profile gen-monitor <model> through the Typer app."""
    models_yaml = minimal_project / "models.yaml"
    models_yaml.write_text(
        models_yaml.read_text()
        + "    axi_bundles: src/axi-bundles.yaml\n"
        + "    axi_monitor_out: gen/axi_perf_mon.sv\n"
    )
    (minimal_project / "src" / "axi-bundles.yaml").write_text(
        "schema_version: '1.0'\nbundles: []\n"
    )

    script, record = _make_fake_profiler(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "axi-profile",
            "gen-monitor",
            "example",
            "-c",
            "models.yaml",
            "--tool",
            str(script),
        ],
    )
    assert result.exit_code == 0, result.output
    argv = json.loads(record.read_text())
    assert argv[0] == "gen-monitor"
    assert argv[1].endswith("src/axi-bundles.yaml")
    assert argv[argv.index("--output") + 1].endswith("gen/axi_perf_mon.sv")


# ---------------------------------------------------------------------------
# RtlBuddyAxiProfileNotebook (unit)
# ---------------------------------------------------------------------------


def _make_fake_marimo(tmp_path: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    """Drop a fake ``marimo`` that records argv + AXI_TXNS_PARQUET env."""
    record = tmp_path / "marimo-invocation.json"
    script = tmp_path / "marimo"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'python - "$@" <<PY\n'
        "import json, os, sys\n"
        f"open({json.dumps(str(record))}, 'w').write(\n"
        "    json.dumps({\n"
        "        'argv': sys.argv[1:],\n"
        "        'env_axi_txns_parquet': os.environ.get('AXI_TXNS_PARQUET'),\n"
        "    })\n"
        ")\n"
        "PY\n"
        f"exit {exit_code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script, record


def _write_notebook_fixture(
    tmp_path: Path, *, parquet_present: bool = True
) -> tuple[Path, Path]:
    """Build a suite_dir with tests.yaml + optional parquet artefact."""
    suite_dir = tmp_path / "verif" / "soc_top"
    suite_dir.mkdir(parents=True)
    src = suite_dir / "src" / "soc.sv"
    src.parent.mkdir()
    src.write_text("module soc; endmodule\n")
    (suite_dir / "models.yaml").write_text(
        "rtl-buddy-filetype: model_config\n"
        "models:\n"
        "  - name: soc\n"
        "    filelist:\n"
        "      - src/soc.sv\n"
    )
    tests_yaml = suite_dir / "tests.yaml"
    tests_yaml.write_text(
        "rtl-buddy-filetype: test_config\n"
        "testbenches:\n"
        "  - name: tb_basic\n"
        "    filelist:\n"
        "      - src/soc.sv\n"
        "tests:\n"
        "  - name: basic\n"
        "    desc: smoke\n"
        "    model: soc\n"
        "    model_path: models.yaml\n"
        "    reglvl: 0\n"
        "    testbench: tb_basic\n"
    )
    if parquet_present:
        parquet_dir = suite_dir / "artefacts" / "axi" / "basic"
        parquet_dir.mkdir(parents=True)
        (parquet_dir / "axi-txns.parquet").write_bytes(b"PAR1\x00\x00stub\x00")
    return suite_dir, tests_yaml


def _notebook_template_or_skip():
    """Skip the happy-path notebook tests when ``rtl_buddy_axi_profiler``
    isn't installed in the test env.

    rtl_buddy uses subprocess-granularity coupling for axi-profiler
    (we shell out to the binary, not import its Python API), so the
    package is intentionally absent in CI. Local dev installs with
    the sibling clone editable-installed exercise these tests.
    """
    return pytest.importorskip("rtl_buddy_axi_profiler.notebook")


def test_notebook_wrapper_builds_expected_argv_and_env(tmp_path: Path) -> None:
    """Lock the marimo argv shape + AXI_TXNS_PARQUET export.

    Downstream (the marimo template) reads the parquet path from
    ``$AXI_TXNS_PARQUET``; if either the env var name or the argv
    shape drifts the user's notebook gets an empty cell and a
    confusing error. Pin both here.
    """
    _notebook_template_or_skip()
    from rtl_buddy.tools.axi_profile_rtl_buddy import RtlBuddyAxiProfileNotebook

    suite_dir, tests_yaml = _write_notebook_fixture(tmp_path)
    script, record = _make_fake_marimo(tmp_path)

    suite_cfg = SuiteConfig(str(tests_yaml))
    test_cfg = suite_cfg.get_tests("basic")[0]

    notebook = RtlBuddyAxiProfileNotebook(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        marimo_executable=str(script),
    )
    assert notebook.run() == 0

    payload = json.loads(record.read_text())
    argv = payload["argv"]
    assert argv[0] == "edit"
    template_path = argv[1]
    assert template_path.endswith("rtl_buddy_axi_profiler/notebook/template.py")
    assert Path(template_path).is_file()
    # AXI_TXNS_PARQUET points at the per-test parquet, not a default.
    parquet_env = payload["env_axi_txns_parquet"]
    assert parquet_env is not None
    assert parquet_env.endswith("artefacts/axi/basic/axi-txns.parquet")
    assert Path(parquet_env).is_file()


def test_notebook_wrapper_forwards_port_flag(tmp_path: Path) -> None:
    _notebook_template_or_skip()
    from rtl_buddy.tools.axi_profile_rtl_buddy import RtlBuddyAxiProfileNotebook

    suite_dir, tests_yaml = _write_notebook_fixture(tmp_path)
    script, record = _make_fake_marimo(tmp_path)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]

    notebook = RtlBuddyAxiProfileNotebook(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        port=2718,
        marimo_executable=str(script),
    )
    assert notebook.run() == 0
    argv = json.loads(record.read_text())["argv"]
    assert argv[argv.index("--port") + 1] == "2718"


def test_notebook_wrapper_forwards_headless_and_no_token(tmp_path: Path) -> None:
    """The hub-initiated flow needs both ``--headless`` (so marimo
    doesn't auto-pop a browser tab while the SPA also tries to open
    the URL) and ``--no-token`` (so the SPA can navigate to the
    printed URL without threading a per-session token through the
    hub → SPA → browser handoff). Lock both as a pair."""
    _notebook_template_or_skip()
    from rtl_buddy.tools.axi_profile_rtl_buddy import RtlBuddyAxiProfileNotebook

    suite_dir, tests_yaml = _write_notebook_fixture(tmp_path)
    script, record = _make_fake_marimo(tmp_path)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]

    notebook = RtlBuddyAxiProfileNotebook(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        headless=True,
        marimo_executable=str(script),
    )
    assert notebook.run() == 0
    argv = json.loads(record.read_text())["argv"]
    assert "--headless" in argv
    assert "--no-token" in argv


def test_notebook_wrapper_omits_headless_by_default(tmp_path: Path) -> None:
    """Default (CLI invocation) keeps marimo's normal token + auto-
    open-browser behaviour — only the hub-initiated path opts in."""
    _notebook_template_or_skip()
    from rtl_buddy.tools.axi_profile_rtl_buddy import RtlBuddyAxiProfileNotebook

    suite_dir, tests_yaml = _write_notebook_fixture(tmp_path)
    script, record = _make_fake_marimo(tmp_path)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]

    notebook = RtlBuddyAxiProfileNotebook(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        marimo_executable=str(script),
    )
    assert notebook.run() == 0
    argv = json.loads(record.read_text())["argv"]
    assert "--headless" not in argv
    assert "--no-token" not in argv


def test_notebook_wrapper_errors_when_parquet_missing(tmp_path: Path) -> None:
    """The user has to run `rb axi-profile run` first — give them
    that exact command in the error so they don't go hunting."""
    from rtl_buddy.tools.axi_profile_rtl_buddy import RtlBuddyAxiProfileNotebook

    suite_dir, tests_yaml = _write_notebook_fixture(tmp_path, parquet_present=False)
    script, _ = _make_fake_marimo(tmp_path)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]

    notebook = RtlBuddyAxiProfileNotebook(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        marimo_executable=str(script),
    )
    with pytest.raises(FatalRtlBuddyError) as exc:
        notebook.run()
    msg = str(exc.value)
    assert "axi-txns.parquet" in msg
    assert "rb axi-profile run basic --emit-txns-parquet" in msg


def test_notebook_wrapper_errors_when_marimo_missing(tmp_path: Path) -> None:
    """When the user hasn't installed the [notebook] extra, the
    marimo binary won't be on PATH. Hint at the install command."""
    _notebook_template_or_skip()
    from rtl_buddy.tools.axi_profile_rtl_buddy import RtlBuddyAxiProfileNotebook

    suite_dir, tests_yaml = _write_notebook_fixture(tmp_path)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]

    # Point at a path that doesn't exist so the path-form branch fires.
    notebook = RtlBuddyAxiProfileNotebook(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        marimo_executable=str(tmp_path / "not-marimo"),
    )
    with pytest.raises(FatalRtlBuddyError) as exc:
        notebook.run()
    assert "marimo" in str(exc.value).lower()


def test_notebook_wrapper_propagates_nonzero_exit(tmp_path: Path) -> None:
    _notebook_template_or_skip()
    from rtl_buddy.tools.axi_profile_rtl_buddy import RtlBuddyAxiProfileNotebook

    suite_dir, tests_yaml = _write_notebook_fixture(tmp_path)
    script, _ = _make_fake_marimo(tmp_path, exit_code=7)
    test_cfg = SuiteConfig(str(tests_yaml)).get_tests("basic")[0]

    notebook = RtlBuddyAxiProfileNotebook(
        name="t",
        test_cfg=test_cfg,
        suite_dir=str(suite_dir),
        marimo_executable=str(script),
    )
    assert notebook.run() == 7


def test_rb_axi_profile_notebook_invokes_stubbed_marimo(
    minimal_project: Path,
) -> None:
    """End-to-end: ``rb axi-profile notebook <test>`` through the
    Typer app, with the parquet pre-staged at the canonical location."""
    _notebook_template_or_skip()
    parquet_dir = minimal_project / "artefacts" / "axi" / "basic"
    parquet_dir.mkdir(parents=True)
    (parquet_dir / "axi-txns.parquet").write_bytes(b"PAR1\x00\x00stub\x00")

    script, record = _make_fake_marimo(minimal_project)
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "axi-profile",
            "notebook",
            "basic",
            "-c",
            "tests.yaml",
            "--marimo",
            str(script),
            "--port",
            "2718",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(record.read_text())
    argv = payload["argv"]
    assert argv[0] == "edit"
    assert argv[1].endswith("rtl_buddy_axi_profiler/notebook/template.py")
    assert argv[argv.index("--port") + 1] == "2718"
    assert payload["env_axi_txns_parquet"].endswith(
        "artefacts/axi/basic/axi-txns.parquet"
    )


def test_rb_axi_profile_notebook_in_subcommand_help(minimal_project: Path) -> None:
    """notebook must appear in `rb axi-profile` --help so users
    discover it without reading the docs."""
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["axi-profile"])
    assert "notebook" in result.output
