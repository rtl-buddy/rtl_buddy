"""Model-native pyslang elaboration flow."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from rtl_buddy.config.elab import ElabConfig, ElabRegConfig
from rtl_buddy.config.dispatch import JobResources
from rtl_buddy.config.model import ModelConfig, ModelConfigLoader
from rtl_buddy.dispatch.argv import elab_job_argv
from rtl_buddy.dispatch.base import ElabJobSpec
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.elab_results import write_elab_result_json
from rtl_buddy.runner.elab_runner import ElabRunner
from rtl_buddy.tool_manifest import ToolStatus, get_manifest, subcommand_readiness


def _cli(*args: str):
    return CliRunner().invoke(RtlBuddy(name="test_elab").app, list(args))


def _write_models(project: Path, models: str) -> Path:
    path = project / "models.yaml"
    path.write_text(models)
    return path


def test_existing_models_yaml_remains_valid_and_path_is_bound(minimal_project: Path):
    loader = ModelConfigLoader(str(minimal_project / "models.yaml"))
    model = loader.get_model("example")
    assert model.elaborations == []
    assert model.path == str(minimal_project / "models.yaml")

    constructed = ModelConfig("core", ["src/core.sv"])
    assert constructed.elaborations == []


def test_profile_loads_from_models_yaml(minimal_project: Path):
    _write_models(
        minimal_project,
        """\
rtl-buddy-filetype: model_config
models:
  - name: core
    filelist: [src/example.sv]
    elaborations:
      - name: smoke
        top: core_top
        reglvl: 2
        single_unit: true
        libraries_inherit_macros: true
        parameters: {WIDTH: 8}
        defines: {FEATURE: 1, TRACE: null}
        resources: {cpus: 2, mem: 1G, time: "00:05:00"}
""",
    )
    model = ModelConfigLoader(str(minimal_project / "models.yaml")).get_model("core")
    profile = model.get_elaboration("smoke")
    assert profile.top == "core_top"
    assert profile.reglvl == 2
    assert profile.parameters == {"WIDTH": 8}
    assert profile.resources.cpus == 2
    assert ElabConfig(model, profile).top == "core_top"


@pytest.mark.parametrize(
    "profile, message",
    [
        ("name: smoke\n        reglvl: -1", "reglvl"),
        (
            "name: smoke\n        libraries_inherit_macros: true",
            "single_unit",
        ),
        ("name: ../escape", "invalid name"),
        ("name: smoke\n        warnings: ['--quiet']", "warning control"),
        ("name: smoke\n        timescale: tomorrow", "timescale"),
        ("name: smoke\n        defines: {FEATURE: 'one two'}", "define.*value"),
        ("name: smoke\n        defines: {FEATURE: 'one+two'}", "define.*value"),
        ("name: smoke\n        resources: {cpus: 0}", "resources.cpus"),
        ("name: base", "reserved name"),
    ],
)
def test_profile_validation_fails_before_running(
    minimal_project: Path, profile: str, message: str
):
    _write_models(
        minimal_project,
        f"""\
rtl-buddy-filetype: model_config
models:
  - name: core
    filelist: [src/example.sv]
    elaborations:
      - {profile}
""",
    )
    with pytest.raises(FatalRtlBuddyError, match=message):
        ModelConfigLoader(str(minimal_project / "models.yaml"))


def test_duplicate_profile_is_rejected(minimal_project: Path):
    _write_models(
        minimal_project,
        """\
rtl-buddy-filetype: model_config
models:
  - name: core
    filelist: [src/example.sv]
    elaborations:
      - name: smoke
      - name: smoke
""",
    )
    with pytest.raises(FatalRtlBuddyError, match="duplicate elaboration profile"):
        ModelConfigLoader(str(minimal_project / "models.yaml"))


def test_bare_model_elaborates_and_writes_structured_artifacts(
    minimal_project: Path,
):
    result = _cli("elab", "example", "-c", "models.yaml")
    assert result.exit_code == 0, result.output

    artifact = minimal_project / "artefacts" / "elab" / "example" / "base"
    expanded = (artifact / "elab.f").read_text()
    assert str(minimal_project / "src" / "example.sv") in expanded
    assert "Command:" in (artifact / "elab.log").read_text()
    envelope = json.loads((artifact / "result.json").read_text())
    assert envelope["rtl-buddy-filetype"] == "elab_result"
    assert envelope["model"] == "example"
    assert envelope["profile"] is None
    payload = envelope["result"]
    assert payload["result"] == "PASS"
    assert payload["top"] == "example"
    assert payload["source_count"] == 1
    assert payload["input_source_count"] == 1
    assert payload["diagnostics"] == {"errors": 0, "warnings": 0}
    assert payload["elapsed_sec"] > 0
    assert payload["peak_memory_bytes"] > 0


def test_bare_model_machine_output_is_one_structured_envelope(
    minimal_project: Path,
):
    result = _cli("--machine", "elab", "example", "-c", "models.yaml")
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert envelope["command"] == "elab"
    assert envelope["exit_code"] == 0
    assert envelope["payload"]["results"][0]["name"] == "example"
    assert envelope["payload"]["results"][0]["result"] == "PASS"
    events = [
        json.loads(line)
        for line in (minimal_project / "rtl_buddy.log").read_text().splitlines()
    ]
    assert any(event.get("event") == "command.elab" for event in events)
    assert any(
        event.get("event") == "elab.verdict" and event.get("result") == "PASS"
        for event in events
    )


def test_profile_applies_sources_defines_parameters_and_warning_controls(
    minimal_project: Path,
):
    src = minimal_project / "src"
    (src / "core.sv").write_text(
        "module core #(parameter int WIDTH = 1);\n"
        '  if (`FEATURE != 1) $error("profile define did not win");\n'
        "  if (WIDTH == 8) helper u();\n"
        "endmodule\n"
    )
    (src / "helper.sv").write_text("module helper; endmodule\n")
    (src / "include").mkdir()
    _write_models(
        minimal_project,
        """\
rtl-buddy-filetype: model_config
models:
  - name: core
    filelist: [+define+FEATURE=0, src/core.sv]
    elaborations:
      - name: smoke
        append_sources: [src/helper.sv]
        include_dirs: [src/include]
        defines: {FEATURE: 1}
        parameters: {WIDTH: 8}
        warnings: [none]
        resources: {cpus: 2}
""",
    )
    result = _cli("elab", "core", "--profile", "smoke", "-c", "models.yaml")
    assert result.exit_code == 0, result.output
    artifact = minimal_project / "artefacts" / "elab" / "core" / "smoke"
    lines = (artifact / "elab.f").read_text().splitlines()
    assert "+define+FEATURE=1" in lines
    assert "+define+FEATURE=0" not in lines
    assert f"+incdir+{src / 'include'}" in lines
    assert lines.index(str(src / "core.sv")) < lines.index(str(src / "helper.sv"))
    log = (artifact / "elab.log").read_text()
    assert "-GWIDTH=8" in log
    assert "-Wnone" in log
    assert "-j=2" in log


def test_hard_diagnostic_still_fails_with_warnings_disabled(minimal_project: Path):
    (minimal_project / "src" / "broken.sv").write_text(
        "module broken; missing_module u(); endmodule\n"
    )
    _write_models(
        minimal_project,
        """\
rtl-buddy-filetype: model_config
models:
  - name: broken
    filelist: [src/broken.sv]
    elaborations:
      - name: smoke
        warnings: [none]
""",
    )
    result = _cli("elab", "broken", "--profile", "smoke", "-c", "models.yaml")
    assert result.exit_code == 1, result.output
    payload = json.loads(
        (
            minimal_project / "artefacts" / "elab" / "broken" / "smoke" / "result.json"
        ).read_text()
    )["result"]
    assert payload["result"] == "FAIL"
    assert payload["diagnostics"]["errors"] > 0


@pytest.mark.parametrize(
    "source, parameters",
    [
        (
            "module child(input logic value); endmodule\n"
            "module broken; child u(.unknown(1'b0)); endmodule\n",
            "{}",
        ),
        ("module broken #(parameter int WIDTH = 1); endmodule\n", "{UNKNOWN: 1}"),
        (
            'module broken; if (1) $error("elaboration guard failed"); endmodule\n',
            "{}",
        ),
    ],
)
def test_elaboration_hard_errors_fail(
    minimal_project: Path, source: str, parameters: str
):
    (minimal_project / "src" / "broken.sv").write_text(source)
    _write_models(
        minimal_project,
        f"""\
rtl-buddy-filetype: model_config
models:
  - name: broken
    filelist: [src/broken.sv]
    elaborations:
      - name: smoke
        parameters: {parameters}
        warnings: [none]
""",
    )

    result = _cli("elab", "broken", "--profile", "smoke", "-c", "models.yaml")
    assert result.exit_code == 1, result.output
    payload = json.loads(
        (
            minimal_project / "artefacts" / "elab" / "broken" / "smoke" / "result.json"
        ).read_text()
    )["result"]
    assert payload["result"] == "FAIL"
    assert payload["diagnostics"]["errors"] > 0


def test_library_macro_inheritance_profile(minimal_project: Path):
    src = minimal_project / "src"
    (src / "core.sv").write_text(
        "`define BUS_WIDTH 8\nmodule core; library_cell u(); endmodule\n"
    )
    (src / "library.sv").write_text(
        "module library_cell; logic [`BUS_WIDTH-1:0] data; endmodule\n"
    )
    _write_models(
        minimal_project,
        """\
rtl-buddy-filetype: model_config
models:
  - name: core
    filelist:
      - src/core.sv
      - -v src/library.sv
    elaborations:
      - name: smoke
        single_unit: true
        libraries_inherit_macros: true
""",
    )
    result = _cli("elab", "core", "--profile", "smoke", "-c", "models.yaml")
    assert result.exit_code == 0, result.output


def test_list_does_not_require_root_or_pyslang(minimal_project: Path):
    _write_models(
        minimal_project,
        """\
rtl-buddy-filetype: model_config
models:
  - name: core
    filelist: [src/example.sv]
    elaborations:
      - name: smoke
""",
    )
    (minimal_project / "root_config.yaml").write_text("not: a valid root config\n")
    result = _cli("elab", "--list", "-c", "models.yaml")
    assert result.exit_code == 0, result.output
    assert "core" in result.output
    assert "core:smoke" in result.output


def test_regression_runs_explicit_profiles_and_records_level_skip(
    minimal_project: Path,
):
    _write_models(
        minimal_project,
        """\
rtl-buddy-filetype: model_config
models:
  - name: example
    filelist: [src/example.sv]
    elaborations:
      - name: smoke
        reglvl: 0
      - name: extended
        reglvl: 2
""",
    )
    (minimal_project / "elab_regression.yaml").write_text(
        "rtl-buddy-filetype: elab_reg_config\nmodel-configs: [models.yaml]\n"
    )
    result = _cli(
        "--machine",
        "elab-regression",
        "-c",
        "elab_regression.yaml",
        "--reg-level",
        "0",
        "--dispatch",
        "local-parallel",
        "--jobs",
        "1",
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert envelope["command"] == "elab-regression"
    assert [row["result"] for row in envelope["payload"]["results"]] == [
        "PASS",
        "SKIP",
    ]
    base = minimal_project / "artefacts" / "elab" / "example"
    assert (
        json.loads((base / "smoke" / "result.json").read_text())["result"]["result"]
        == "PASS"
    )
    assert (
        json.loads((base / "extended" / "result.json").read_text())["result"]["result"]
        == "SKIP"
    )


def test_regression_falls_back_to_root_config_manifest(minimal_project: Path):
    _write_models(
        minimal_project,
        """\
rtl-buddy-filetype: model_config
models:
  - name: example
    filelist: [src/example.sv]
    elaborations:
      - name: smoke
""",
    )
    manifest_dir = minimal_project / "flows" / "elab"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "profiles.yaml").write_text(
        "rtl-buddy-filetype: elab_reg_config\nmodel-configs: [../../models.yaml]\n"
    )
    root = minimal_project / "root_config.yaml"
    root.write_text(
        root.read_text().replace(
            'reg-cfg-path: "regression.yaml"',
            'reg-cfg-path: "regression.yaml"\n'
            '  elab-reg-cfg-path: "flows/elab/profiles.yaml"',
        )
    )

    result = _cli("elab-regression")
    assert result.exit_code == 0, result.output
    assert (
        json.loads(
            (
                minimal_project
                / "artefacts"
                / "elab"
                / "example"
                / "smoke"
                / "result.json"
            ).read_text()
        )["result"]["result"]
        == "PASS"
    )


def test_regression_manifest_requires_an_explicit_profile(minimal_project: Path):
    (minimal_project / "elab_regression.yaml").write_text(
        "rtl-buddy-filetype: elab_reg_config\nmodel-configs: [models.yaml]\n"
    )
    with pytest.raises(FatalRtlBuddyError, match="no named profiles"):
        ElabRegConfig("reg", str(minimal_project / "elab_regression.yaml"))


def test_local_parallel_dispatch_uses_the_same_worker(minimal_project: Path):
    result = _cli(
        "elab",
        "example",
        "-c",
        "models.yaml",
        "--dispatch",
        "local-parallel",
        "--jobs",
        "1",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(
        (
            minimal_project / "artefacts" / "elab" / "example" / "base" / "result.json"
        ).read_text()
    )["result"]
    assert payload["result"] == "PASS"


def test_direct_dispatch_is_opt_in(minimal_project: Path):
    root = minimal_project / "root_config.yaml"
    root.write_text(
        root.read_text() + "\ncfg-dispatch:\n  backend: local-parallel\n  jobs: 1\n"
    )

    direct = _cli("elab", "example", "-c", "models.yaml")
    assert direct.exit_code == 0, direct.output
    assert not (
        minimal_project / "artefacts" / "elab" / "example" / "base" / "dispatch"
    ).exists()


def test_regression_honors_root_dispatch_config(minimal_project: Path):
    _write_models(
        minimal_project,
        """\
rtl-buddy-filetype: model_config
models:
  - name: example
    filelist: [src/example.sv]
    elaborations:
      - name: smoke
""",
    )
    (minimal_project / "elab_regression.yaml").write_text(
        "rtl-buddy-filetype: elab_reg_config\nmodel-configs: [models.yaml]\n"
    )
    root = minimal_project / "root_config.yaml"
    root.write_text(
        root.read_text() + "\ncfg-dispatch:\n  backend: local-parallel\n  jobs: 1\n"
    )
    regression = _cli("elab-regression", "-c", "elab_regression.yaml")
    assert regression.exit_code == 0, regression.output
    assert (
        minimal_project / "artefacts" / "elab" / "example" / "smoke" / "dispatch"
    ).is_dir()


def test_dispatched_regression_resolves_resources_under_each_model_root(
    minimal_project: Path, monkeypatch: pytest.MonkeyPatch
):
    import shutil

    from rtl_buddy.dispatch.local_parallel import LocalProcessBackend

    profile = """\
rtl-buddy-filetype: model_config
models:
  - name: example
    filelist: [src/example.sv]
    elaborations:
      - name: smoke
"""
    _write_models(minimal_project, profile)
    other = minimal_project / "other"
    shutil.copytree(minimal_project, other, ignore=shutil.ignore_patterns("other"))
    _write_models(other, profile)
    (other / "root_config.yaml").write_text(
        (other / "root_config.yaml").read_text()
        + "\ncfg-dispatch:\n  resources: {cpus: 3, mem: 7G, time: '00:07:00'}\n"
    )
    (minimal_project / "elab_regression.yaml").write_text(
        "rtl-buddy-filetype: elab_reg_config\n"
        "model-configs: [models.yaml, other/models.yaml]\n"
    )

    captured = []
    original = LocalProcessBackend.submit_array

    def capture(self, specs, **kwargs):
        captured.extend(specs)
        return original(self, specs, **kwargs)

    monkeypatch.setattr(LocalProcessBackend, "submit_array", capture)
    result = _cli(
        "elab-regression",
        "-c",
        "elab_regression.yaml",
        "--dispatch",
        "local-parallel",
        "--jobs",
        "1",
    )
    assert result.exit_code == 0, result.output
    by_root = {Path(spec.model_config_path).parent: spec for spec in captured}
    assert set(by_root) == {minimal_project.resolve(), other.resolve()}
    assert by_root[minimal_project.resolve()].resources == JobResources()
    assert by_root[other.resolve()].resources == JobResources(
        cpus=3, mem="7G", time="00:07:00"
    )


def test_worker_failure_cannot_reuse_a_stale_pass(
    minimal_project: Path, monkeypatch: pytest.MonkeyPatch
):
    import rtl_buddy.runner.elab_runner as elab_runner_module

    model = ModelConfigLoader(str(minimal_project / "models.yaml")).get_model("example")
    cfg = ElabConfig(model)
    result_path = cfg.artifact_dir / "result.json"
    write_elab_result_json(
        result_path,
        model="example",
        profile=None,
        results={"result": "PASS", "desc": "stale"},
    )
    monkeypatch.setattr(
        elab_runner_module,
        "run_managed_process",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=9),
    )

    result = ElabRunner(root_cfg=None, elab_cfg=cfg, resources=JobResources()).run()
    assert result.results["result"] == "FAIL"
    assert "did not produce a result" in result.results["desc"]


def test_elab_dispatch_argv_is_machine_mode_and_carries_resources():
    spec = ElabJobSpec(
        model_name="core",
        profile_name="smoke",
        suite_dir="/project/design",
        model_config_path="/project/design/models.yaml",
        result_json=Path("/project/design/result.json"),
    )
    spec.resources.cpus = 3
    argv = elab_job_argv(spec)
    assert argv[:4] == [argv[0], "-m", "rtl_buddy", "--machine"]
    assert "_elab-job" in argv
    assert argv[argv.index("--cpus") + 1] == "3"
    assert argv[argv.index("--profile") + 1] == "smoke"


def test_pyslang_is_required_for_elaboration_readiness():
    specs = get_manifest()
    statuses = [
        ToolStatus(
            name=spec.name,
            status="missing" if spec.name == "pyslang" else "ok",
            version=None,
            path=None,
            optional=spec.optional,
            minimum_version=spec.minimum_version,
            kind=None,
            used_by=spec.used_by,
        )
        for spec in specs
    ]
    readiness = subcommand_readiness(statuses, specs)
    assert readiness["elab"]["status"] == "missing"
    assert readiness["elab-regression"]["missing"] == ["pyslang"]
    assert readiness["hier"]["status"] == "ok"


def test_focused_tool_check_keeps_required_optional_dependency():
    result = _cli(
        "tool-check",
        "--required-for",
        "elab",
        "--no-include-optional",
        "--no-probe-versions",
        "--format",
        "json",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert list(payload["tools"]) == ["pyslang"]
    assert payload["subcommands"]["elab"]["status"] == "ok"
