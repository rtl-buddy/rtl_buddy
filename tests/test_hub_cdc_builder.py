"""Tests for the hub-side cdc → domain_map builder.

The real path invokes ``rtl-buddy-cdc lint --emit-domain-map`` which
we don't want to require in CI; the subprocess is mocked. Tests pin:

  - back-pointer resolution (no field, with fragment, by model match)
  - error paths (file missing, multiple analyses, missing SDC)
  - subprocess command shape (--top, --sdc, --emit-domain-map, sources)
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from rtl_buddy.config.model import ModelConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.hub import cdc_builder


_MODELS_YAML = dedent("""\
    rtl-buddy-filetype: model_config
    models:
      - name: demo
        filelist: ["-v src/a.sv"]
        cdc: cdc.yaml
""")

_CDC_YAML_TEMPLATE = dedent("""\
    rtl-buddy-filetype: cdc_config

    analyses:
      - name: "{analysis_name}"
        desc: "demo cdc"
        model: "demo"
        model_path: "models.yaml"
        tool: "rtl-buddy-cdc"
        constraints: "demo.sdc"
""")

_CDC_YAML_MULTI = dedent("""\
    rtl-buddy-filetype: cdc_config

    analyses:
      - name: "fast"
        desc: "fast corner"
        model: "demo"
        model_path: "models.yaml"
        tool: "rtl-buddy-cdc"
        constraints: "demo.sdc"
      - name: "slow"
        desc: "slow corner"
        model: "demo"
        model_path: "models.yaml"
        tool: "rtl-buddy-cdc"
        constraints: "demo.sdc"
""")


def _seed_project(tmp_path: Path, *, cdc_field: str = "cdc.yaml") -> ModelConfig:
    """Create a project skeleton with models.yaml + cdc.yaml + SDC +
    one source file, and return a ModelConfig pointing at it (with
    ``.path`` set so the cdc back-pointer can resolve)."""
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "a.sv").write_text("module a; endmodule\n")
    (tmp_path / "demo.sdc").write_text(
        "create_clock -name clk -period 10 [get_ports clk]\n"
    )
    models_path = tmp_path / "models.yaml"
    body = dedent(f"""\
        rtl-buddy-filetype: model_config
        models:
          - name: demo
            filelist: ["-v src/a.sv"]
            cdc: {cdc_field}
    """)
    models_path.write_text(body)
    return ModelConfig(
        name="demo",
        filelist=["-v src/a.sv"],
        cdc=cdc_field,
        path=str(models_path),
    )


def test_domain_map_path_under_cache_dir(tmp_path):
    assert (
        cdc_builder.domain_map_path(tmp_path, "demo")
        == tmp_path / ".rtl-buddy" / "cache" / "domain-demo.json"
    )


def test_build_domain_map_no_cdc_field_returns_none(tmp_path):
    """A model without a ``cdc:`` back-pointer means "no overlay
    requested" — the builder returns ``None`` and the caller skips
    rtl-buddy-view's ``--cdc-annotations`` flag."""
    model = ModelConfig(name="demo", filelist=[], path=str(tmp_path / "models.yaml"))
    assert cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model) is None


def test_build_domain_map_missing_cdc_yaml_raises(tmp_path):
    """``cdc: cdc.yaml`` set but no such file → fail loud at hub
    start. Better than silently dropping the overlay."""
    model = _seed_project(tmp_path)
    # Don't write the cdc.yaml file
    with pytest.raises(FatalRtlBuddyError, match="cdc back-pointer.*does not exist"):
        cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)


def test_build_domain_map_resolves_via_model_match(tmp_path, monkeypatch):
    """Without a ``#fragment`` the builder picks the analysis whose
    ``model:`` field matches the model's name."""
    model = _seed_project(tmp_path)
    (tmp_path / "cdc.yaml").write_text(
        _CDC_YAML_TEMPLATE.format(analysis_name="demo_cdc")
    )

    captured = {}

    monkeypatch.setattr(cdc_builder.shutil, "which", lambda _: "/fake/rtl-buddy-cdc")

    def fake_run(cmd, stdout=None, stderr=None, **kwargs):
        captured["cmd"] = cmd
        # Pretend cdc wrote the file.
        out = cmd[cmd.index("--emit-domain-map") + 1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text('{"schema_version": "1.0", "clocks": []}')
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(cdc_builder.subprocess, "run", fake_run)

    result = cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)
    assert result == cdc_builder.domain_map_path(tmp_path, "demo")
    assert result.is_file()
    cmd = captured["cmd"]
    assert cmd[0] == "/fake/rtl-buddy-cdc"
    assert "lint" in cmd
    assert "--top" in cmd
    assert "demo" in cmd
    assert "--sdc" in cmd
    assert "--emit-domain-map" in cmd
    # SDC should be the absolute path resolved against cdc.yaml.
    sdc_idx = cmd.index("--sdc")
    assert Path(cmd[sdc_idx + 1]) == tmp_path / "demo.sdc"


def test_build_domain_map_honours_fragment(tmp_path, monkeypatch):
    """``cdc: cdc.yaml#slow`` pins one analysis even when there are
    multiple candidates."""
    model = _seed_project(tmp_path, cdc_field="cdc.yaml#slow")
    (tmp_path / "cdc.yaml").write_text(_CDC_YAML_MULTI)

    captured = {}
    monkeypatch.setattr(cdc_builder.shutil, "which", lambda _: "/fake/rtl-buddy-cdc")

    def fake_run(cmd, stdout=None, stderr=None, **kwargs):
        captured["cmd"] = cmd
        out = cmd[cmd.index("--emit-domain-map") + 1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text("{}")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(cdc_builder.subprocess, "run", fake_run)
    cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)
    # ``--top`` is "demo" for both analyses, but the analysis NAME
    # used in error messages / paths comes from the fragment. We
    # don't expose the analysis name on the command line, so the
    # easiest signal is that the call didn't raise the
    # "multiple analyses" error.
    assert "--emit-domain-map" in captured["cmd"]


def test_build_domain_map_ambiguous_without_fragment_raises(tmp_path):
    """Two analyses, same model, no fragment → tell the user to
    pick one with a #fragment in models.yaml."""
    model = _seed_project(tmp_path)
    (tmp_path / "cdc.yaml").write_text(_CDC_YAML_MULTI)
    with pytest.raises(FatalRtlBuddyError, match="multiple analyses"):
        cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)


def test_build_domain_map_missing_sdc_raises(tmp_path):
    """Analysis points at an SDC file that doesn't exist → fail loud
    (vs. letting rtl-buddy-cdc emit a confusing error downstream)."""
    model = _seed_project(tmp_path)
    (tmp_path / "demo.sdc").unlink()
    (tmp_path / "cdc.yaml").write_text(
        _CDC_YAML_TEMPLATE.format(analysis_name="demo_cdc")
    )
    with pytest.raises(FatalRtlBuddyError, match="SDC not found"):
        cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)


def test_build_domain_map_missing_executable_raises(tmp_path, monkeypatch):
    model = _seed_project(tmp_path)
    (tmp_path / "cdc.yaml").write_text(
        _CDC_YAML_TEMPLATE.format(analysis_name="demo_cdc")
    )
    monkeypatch.setattr(cdc_builder.shutil, "which", lambda _: None)
    with pytest.raises(FatalRtlBuddyError, match="rtl-buddy-cdc.*not on PATH"):
        cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)


def test_build_domain_map_subprocess_failure_raises(tmp_path, monkeypatch):
    """Non-zero exit (other than 1, which is "violations") → fail."""
    model = _seed_project(tmp_path)
    (tmp_path / "cdc.yaml").write_text(
        _CDC_YAML_TEMPLATE.format(analysis_name="demo_cdc")
    )
    monkeypatch.setattr(cdc_builder.shutil, "which", lambda _: "/fake/rtl-buddy-cdc")

    def fake_run(cmd, stdout=None, stderr=None, **kwargs):
        return type("R", (), {"returncode": 7})()

    monkeypatch.setattr(cdc_builder.subprocess, "run", fake_run)
    with pytest.raises(FatalRtlBuddyError, match=r"exited with code 7"):
        cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)


def test_build_domain_map_tolerates_violations_exit_1(tmp_path, monkeypatch):
    """CDC exit-1 means rule violations were found — the elaboration
    still succeeded and the domain map was emitted. The hub doesn't
    care about lint violations; the overlay should work."""
    model = _seed_project(tmp_path)
    (tmp_path / "cdc.yaml").write_text(
        _CDC_YAML_TEMPLATE.format(analysis_name="demo_cdc")
    )
    monkeypatch.setattr(cdc_builder.shutil, "which", lambda _: "/fake/rtl-buddy-cdc")

    def fake_run(cmd, stdout=None, stderr=None, **kwargs):
        out = cmd[cmd.index("--emit-domain-map") + 1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text("{}")
        return type("R", (), {"returncode": 1})()

    monkeypatch.setattr(cdc_builder.subprocess, "run", fake_run)
    result = cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)
    assert result is not None
    assert result.is_file()
