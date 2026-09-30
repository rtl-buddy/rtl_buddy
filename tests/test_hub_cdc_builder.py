"""Tests for the hub-side cdc -> domain_map builder.

``rtl-buddy-cdc lint --emit-domain-map`` is mocked. The tests pin back-pointer
resolution, error paths and the subprocess command shape.
"""

from __future__ import annotations

import logging
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


def _seed_project(
    tmp_path: Path, *, cdc_field: str = "cdc.yaml", top: str | None = None
) -> ModelConfig:
    """Create a project skeleton (models.yaml, cdc.yaml, SDC, one source).

    ``top`` writes a models.yaml ``top:`` override, which the analysis inherits when
    the suite re-loads the model.
    """
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "a.sv").write_text("module a; endmodule\n")
    (tmp_path / "demo.sdc").write_text(
        "create_clock -name clk -period 10 [get_ports clk]\n"
    )
    models_path = tmp_path / "models.yaml"
    top_line = f"    top: {top}\n" if top else ""
    body = (
        dedent(f"""\
        rtl-buddy-filetype: model_config
        models:
          - name: demo
            filelist: ["-v src/a.sv"]
            cdc: {cdc_field}
    """)
        + top_line
    )
    models_path.write_text(body)
    return ModelConfig(
        name="demo",
        filelist=["-v src/a.sv"],
        cdc=cdc_field,
        top=top,
        path=str(models_path),
    )


def test_domain_map_path_under_cache_dir(tmp_path):
    assert (
        cdc_builder.domain_map_path(tmp_path, "demo")
        == tmp_path / ".rtl-buddy" / "cache" / "domain-demo.json"
    )


def test_build_domain_map_no_cdc_field_returns_none(tmp_path):
    """A model without a ``cdc:`` back-pointer requests no overlay, so the builder
    returns ``None``."""
    model = ModelConfig(name="demo", filelist=[], path=str(tmp_path / "models.yaml"))
    assert cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model) is None


def test_build_domain_map_missing_cdc_yaml_raises(tmp_path):
    """``cdc: cdc.yaml`` with no such file fails loudly."""
    model = _seed_project(tmp_path)
    # Don't write the cdc.yaml file.
    with pytest.raises(FatalRtlBuddyError, match="cdc back-pointer.*does not exist"):
        cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)


def test_build_domain_map_resolves_via_model_match(tmp_path, monkeypatch):
    """Without a ``#fragment`` the builder picks the analysis whose ``model:`` matches
    the model's name."""
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
    # The SDC path is absolute, resolved against cdc.yaml.
    sdc_idx = cmd.index("--sdc")
    assert Path(cmd[sdc_idx + 1]) == tmp_path / "demo.sdc"


def test_build_domain_map_warns_on_filelist_incdirs(tmp_path, monkeypatch, caplog):
    """A model `+incdir+` is reported, since rtl-buddy-cdc has no include-path option."""
    model = _seed_project(tmp_path)
    (tmp_path / "inc").mkdir()
    model.filelist.insert(0, "+incdir+inc")
    (tmp_path / "cdc.yaml").write_text(
        _CDC_YAML_TEMPLATE.format(analysis_name="demo_cdc")
    )
    monkeypatch.setattr(cdc_builder.shutil, "which", lambda _: "/fake/rtl-buddy-cdc")

    def fake_run(cmd, stdout=None, stderr=None, **kwargs):
        out = cmd[cmd.index("--emit-domain-map") + 1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text('{"schema_version": "1.0", "clocks": []}')
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(cdc_builder.subprocess, "run", fake_run)

    with caplog.at_level(logging.WARNING, logger="rtl_buddy"):
        cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)

    events = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "cdc.filelist_incdirs_unsupported"
    ]
    assert len(events) == 1
    assert events[0].rtl_fields["analysis"] == "demo_cdc"
    assert events[0].rtl_fields["incdirs"] == [str(tmp_path / "inc")]


def test_build_domain_map_resolves_a_model_with_a_top_override(tmp_path, monkeypatch):
    """A models.yaml ``top:`` does not break back-pointer resolution.

    Analyses are selected by the model they name, not by the module they root at.
    The lint call still roots at the override.
    """
    model = _seed_project(tmp_path, top="axi_xbar")
    (tmp_path / "cdc.yaml").write_text(
        _CDC_YAML_TEMPLATE.format(analysis_name="demo_cdc")
    )

    captured = {}
    monkeypatch.setattr(cdc_builder.shutil, "which", lambda _: "/fake/rtl-buddy-cdc")

    def fake_run(cmd, stdout=None, stderr=None, **kwargs):
        captured["cmd"] = cmd
        out = cmd[cmd.index("--emit-domain-map") + 1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text('{"schema_version": "1.0", "clocks": []}')
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(cdc_builder.subprocess, "run", fake_run)

    result = cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)
    assert result == cdc_builder.domain_map_path(tmp_path, "demo")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--top") + 1] == "axi_xbar"


def test_build_domain_map_honours_fragment(tmp_path, monkeypatch):
    """``cdc: cdc.yaml#slow`` pins one analysis among several."""
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
    # ``--top`` is "demo" for both analyses, so the signal is that the "multiple
    # analyses" error was not raised.
    assert "--emit-domain-map" in captured["cmd"]


def test_build_domain_map_ambiguous_without_fragment_raises(tmp_path):
    """Two analyses for one model without a fragment raise, telling the user to add a
    #fragment."""
    model = _seed_project(tmp_path)
    (tmp_path / "cdc.yaml").write_text(_CDC_YAML_MULTI)
    with pytest.raises(FatalRtlBuddyError, match="multiple analyses"):
        cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)


def test_build_domain_map_missing_sdc_raises(tmp_path):
    """An analysis pointing at a missing SDC file raises."""
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
    """A non-zero exit other than 1 raises."""
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
    """Exit 1 means CDC rule violations; the domain map was still emitted and is used."""
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


def test_build_domain_map_ignores_a_previous_builds_cache(tmp_path, monkeypatch):
    """A stale map in the persistent `.rtl-buddy/cache/` is ignored when the analyzer
    crashes with exit 1."""
    model = _seed_project(tmp_path)
    (tmp_path / "cdc.yaml").write_text(
        _CDC_YAML_TEMPLATE.format(analysis_name="demo_cdc")
    )

    stale = cdc_builder.domain_map_path(tmp_path, "demo")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"schema_version": "1.0", "clocks": ["stale_clk"]}')

    monkeypatch.setattr(cdc_builder.shutil, "which", lambda _: "/fake/rtl-buddy-cdc")

    def fake_run(cmd, stdout=None, stderr=None, **kwargs):
        # Crashes with the "rule violations found" code, writing nothing.
        return type("R", (), {"returncode": 1})()

    monkeypatch.setattr(cdc_builder.subprocess, "run", fake_run)

    with pytest.raises(FatalRtlBuddyError, match="produced no domain map"):
        cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)
    assert not stale.exists()


def test_build_domain_map_still_accepts_a_map_this_build_wrote(tmp_path, monkeypatch):
    """A map the current invocation writes is still returned on the tolerated exit 1."""
    model = _seed_project(tmp_path)
    (tmp_path / "cdc.yaml").write_text(
        _CDC_YAML_TEMPLATE.format(analysis_name="demo_cdc")
    )

    stale = cdc_builder.domain_map_path(tmp_path, "demo")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"clocks": ["stale_clk"]}')

    monkeypatch.setattr(cdc_builder.shutil, "which", lambda _: "/fake/rtl-buddy-cdc")

    def fake_run(cmd, stdout=None, stderr=None, **kwargs):
        out = cmd[cmd.index("--emit-domain-map") + 1]
        Path(out).write_text('{"clocks": ["fresh_clk"]}')
        return type("R", (), {"returncode": 1})()

    monkeypatch.setattr(cdc_builder.subprocess, "run", fake_run)

    result = cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)
    assert "fresh_clk" in result.read_text()


def _seed_cached_map(tmp_path) -> Path:
    """A domain map left in the persistent cache by an earlier build."""
    stale = cdc_builder.domain_map_path(tmp_path, "demo")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"schema_version": "1.0", "clocks": ["stale_clk"]}')
    return stale


def test_build_domain_map_clears_the_cache_before_back_pointer_resolution(tmp_path):
    """The cache is cleared before back-pointer resolution, so a bad `cdc:` never leaves
    the previous overlay served."""
    model = _seed_project(tmp_path, cdc_field="does_not_exist.yaml")
    stale = _seed_cached_map(tmp_path)

    with pytest.raises(FatalRtlBuddyError):
        cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)

    assert not stale.exists()


def test_build_domain_map_clears_the_cache_when_the_sdc_is_missing(tmp_path):
    """The cache is cleared when the SDC is missing."""
    model = _seed_project(tmp_path)
    (tmp_path / "cdc.yaml").write_text(
        _CDC_YAML_TEMPLATE.format(analysis_name="demo_cdc")
    )
    (tmp_path / "demo.sdc").unlink()
    stale = _seed_cached_map(tmp_path)

    with pytest.raises(FatalRtlBuddyError, match="SDC not found"):
        cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)

    assert not stale.exists()


def test_build_domain_map_clears_the_cache_when_the_analyzer_is_absent(
    tmp_path, monkeypatch
):
    """The cache is cleared when the analyzer is absent; there is no missing-tool carve-
    out for this derived cache."""
    model = _seed_project(tmp_path)
    (tmp_path / "cdc.yaml").write_text(
        _CDC_YAML_TEMPLATE.format(analysis_name="demo_cdc")
    )
    stale = _seed_cached_map(tmp_path)

    monkeypatch.setattr(cdc_builder.shutil, "which", lambda _: None)

    with pytest.raises(FatalRtlBuddyError):
        cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)

    assert not stale.exists()


def test_build_domain_map_clears_the_cache_when_the_back_pointer_is_gone(tmp_path):
    """The cache is cleared when the model drops its overlay request."""
    model = ModelConfig(name="demo", filelist=[], path=str(tmp_path / "models.yaml"))
    stale = _seed_cached_map(tmp_path)

    assert cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model) is None
    assert not stale.exists()


def test_build_domain_map_clears_a_map_the_analyzer_wrote_then_rejected(
    tmp_path, monkeypatch
):
    """A map the analyzer wrote before an unsupported exit code is cleared afterwards
    too."""
    model = _seed_project(tmp_path)
    (tmp_path / "cdc.yaml").write_text(
        _CDC_YAML_TEMPLATE.format(analysis_name="demo_cdc")
    )
    out_path = cdc_builder.domain_map_path(tmp_path, "demo")

    monkeypatch.setattr(cdc_builder.shutil, "which", lambda _: "/fake/rtl-buddy-cdc")

    def _writes_then_dies(cmd, stdout=None, stderr=None, **kwargs):
        target = Path(cmd[cmd.index("--emit-domain-map") + 1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"clocks": ["half built"]}')
        return type("R", (), {"returncode": 2})()

    monkeypatch.setattr(cdc_builder.subprocess, "run", _writes_then_dies)

    with pytest.raises(FatalRtlBuddyError, match="exited with code 2"):
        cdc_builder.build_domain_map(project_root=tmp_path, model_cfg=model)

    assert not out_path.exists()
