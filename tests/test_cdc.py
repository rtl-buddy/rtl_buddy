"""Tests for the CDC config surface: tool config, per-analysis config,
suite/regression YAML loading. Mirrors the structure of ``test_synth.py``.
"""

from pathlib import Path
from textwrap import dedent

import pytest

from rtl_buddy.config.cdc import (
    CdcConfig,
    CdcRegConfig,
    CdcSuiteConfig,
    CdcToolConfig,
    CdcToolConfigFile,
    CdcToolOptsFile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_cfg(
    name="rtl-buddy-cdc", exe="rtl-buddy-cdc", sync_depth=None, extra_args=""
):
    opts = CdcToolOptsFile(sync_depth=sync_depth, extra_args=extra_args)
    return CdcToolConfig(CdcToolConfigFile(name=name, tool=exe, opts=opts))


def _make_cdc_cfg(
    *,
    name="test_cdc",
    model_name="my_module",
    model_path="/fake/models.yaml",
    tool="rtl-buddy-cdc",
    constraints="my_module.sdc",
    waivers=None,
    reglvl=None,
    tool_overrides=None,
    frontend=None,
):
    from rtl_buddy.config.model import ModelConfig

    model = ModelConfig(name=model_name, filelist=[], path=model_path)
    return CdcConfig(
        name=name,
        desc="test cdc",
        model=model,
        tool=tool,
        constraints=constraints,
        waivers=waivers,
        _reglvl=reglvl,
        tool_overrides=tool_overrides,
        frontend=frontend,
    )


# ---------------------------------------------------------------------------
# CdcToolConfig — opts and overrides
# ---------------------------------------------------------------------------


def test_cdc_tool_config_returns_base_opts():
    cfg = _tool_cfg(sync_depth=3, extra_args="--strict")
    opts = cfg.get_opts()
    assert opts.sync_depth == 3
    assert opts.extra_args == "--strict"


def test_cdc_tool_config_overrides_merge_over_base():
    cfg = _tool_cfg(sync_depth=2, extra_args="")
    opts = cfg.get_opts({"sync_depth": 4, "extra_args": "--debug"})
    assert opts.sync_depth == 4
    assert opts.extra_args == "--debug"


def test_cdc_tool_config_partial_override_keeps_unset_base():
    cfg = _tool_cfg(sync_depth=2, extra_args="--baseline")
    opts = cfg.get_opts({"sync_depth": 4})
    assert opts.sync_depth == 4
    assert opts.extra_args == "--baseline"  # unchanged


def test_cdc_tool_config_none_override_returns_base():
    cfg = _tool_cfg(sync_depth=2)
    assert cfg.get_opts(None).sync_depth == 2
    assert cfg.get_opts({}).sync_depth == 2


# ---------------------------------------------------------------------------
# CdcConfig — reglvl semantics (mirrors synth's int/dict/default behavior)
# ---------------------------------------------------------------------------


def test_cdc_config_top_is_model_name():
    cfg = _make_cdc_cfg(model_name="my_top")
    assert cfg.get_top() == "my_top"


def test_cdc_config_reglvl_int():
    cfg = _make_cdc_cfg(reglvl=500)
    assert cfg.get_reglvl("rtl-buddy-cdc") == 500


def test_cdc_config_reglvl_none_defaults_to_zero():
    cfg = _make_cdc_cfg(reglvl=None)
    assert cfg.get_reglvl("rtl-buddy-cdc") == 0


def test_cdc_config_reglvl_dict_tool_specific():
    cfg = _make_cdc_cfg(
        reglvl={"rtl-buddy-cdc": 100, "spyglass-cdc": 200, "default": 50}
    )
    assert cfg.get_reglvl("rtl-buddy-cdc") == 100
    assert cfg.get_reglvl("spyglass-cdc") == 200
    assert cfg.get_reglvl("questa-cdc") == 50  # falls back to default


def test_cdc_config_reglvl_dict_default_only():
    """A dict with only `default` must be honored for any tool."""
    cfg = _make_cdc_cfg(reglvl={"default": 100})
    assert cfg.get_reglvl("rtl-buddy-cdc") == 100
    assert cfg.get_reglvl("anything") == 100


def test_cdc_config_reglvl_malformed_dict_raises():
    """A dict with neither the active tool nor `default` is malformed."""
    from rtl_buddy.errors import FatalRtlBuddyError

    cfg = _make_cdc_cfg(reglvl={"some-other-tool": 100})
    with pytest.raises(FatalRtlBuddyError, match="reglvl"):
        cfg.get_reglvl("rtl-buddy-cdc")


# ---------------------------------------------------------------------------
# CdcConfig — tool_overrides (nested by tool name)
# ---------------------------------------------------------------------------


def test_cdc_config_tool_overrides_for_matching_tool():
    cfg = _make_cdc_cfg(tool_overrides={"rtl-buddy-cdc": {"extra_args": "--strict"}})
    assert cfg.get_tool_overrides_for("rtl-buddy-cdc") == {"extra_args": "--strict"}


def test_cdc_config_tool_overrides_for_non_matching_tool():
    cfg = _make_cdc_cfg(tool_overrides={"rtl-buddy-cdc": {"extra_args": "--strict"}})
    assert cfg.get_tool_overrides_for("spyglass-cdc") is None


def test_cdc_config_tool_overrides_none():
    cfg = _make_cdc_cfg(tool_overrides=None)
    assert cfg.get_tool_overrides_for("rtl-buddy-cdc") is None


def test_cdc_config_tool_overrides_merge_through_tool_cfg():
    """End-to-end: a per-analysis tool_overrides entry overrides the root
    config baseline when passed through CdcToolConfig.get_opts()."""
    cdc_cfg = _make_cdc_cfg(
        tool_overrides={"rtl-buddy-cdc": {"sync_depth": 4, "extra_args": "--strict"}}
    )
    tool_cfg = _tool_cfg(sync_depth=2, extra_args="")
    opts = tool_cfg.get_opts(cdc_cfg.get_tool_overrides_for(tool_cfg.get_name()))
    assert opts.sync_depth == 4
    assert opts.extra_args == "--strict"


# ---------------------------------------------------------------------------
# CdcConfig — frontend (per-analysis elaboration frontend selector)
# ---------------------------------------------------------------------------


def test_cdc_config_frontend_defaults_to_none():
    cfg = _make_cdc_cfg()
    assert cfg.frontend is None


def test_cdc_config_frontend_explicit_slang():
    cfg = _make_cdc_cfg(frontend="slang")
    assert cfg.frontend == "slang"


def test_cdc_config_frontend_explicit_yosys():
    cfg = _make_cdc_cfg(frontend="yosys")
    assert cfg.frontend == "yosys"


# ---------------------------------------------------------------------------
# CdcSuiteConfig — YAML loading + path resolution
# ---------------------------------------------------------------------------

_SUITE_YAML = dedent("""\
    rtl-buddy-filetype: cdc_config

    analyses:
      - name: "cdc_a"
        desc: "First analysis"
        model: "mod_a"
        model_path: "{models_path}"
        tool: "rtl-buddy-cdc"
        constraints: "mod_a.sdc"
        reglvl: 0
      - name: "cdc_b"
        desc: "Second analysis"
        model: "mod_b"
        model_path: "{models_path}"
        tool: "rtl-buddy-cdc"
        constraints: "mod_b.sdc"
        waivers: "mod_b.waivers"
        reglvl: 1000
        frontend: "slang"
""")

_MODELS_YAML = dedent("""\
    rtl-buddy-filetype: model_config

    models:
      - name: "mod_a"
        filelist: ["top_a.sv"]
      - name: "mod_b"
        filelist: ["top_b.sv"]
""")


def _write_suite(tmp_path):
    (tmp_path / "models.yaml").write_text(_MODELS_YAML)
    suite_yaml = tmp_path / "cdc.yaml"
    suite_yaml.write_text(_SUITE_YAML.format(models_path="models.yaml"))
    return suite_yaml


def test_cdc_suite_config_loads_all_analyses(tmp_path):
    suite_yaml = _write_suite(tmp_path)
    cfg = CdcSuiteConfig(str(suite_yaml))
    assert cfg.get_analysis_names() == ["cdc_a", "cdc_b"]


def test_cdc_suite_config_get_by_name(tmp_path):
    suite_yaml = _write_suite(tmp_path)
    cfg = CdcSuiteConfig(str(suite_yaml))
    results = cfg.get_analyses("cdc_a")
    assert len(results) == 1
    assert results[0].get_name() == "cdc_a"
    assert results[0].get_top() == "mod_a"


def test_cdc_suite_config_paths_resolved_relative_to_yaml(tmp_path):
    """constraints and waivers paths must be resolved relative to the
    cdc.yaml file (matches the synth/test convention)."""
    suite_yaml = _write_suite(tmp_path)
    cfg = CdcSuiteConfig(str(suite_yaml))
    cdc_a = cfg.get_analyses("cdc_a")[0]
    cdc_b = cfg.get_analyses("cdc_b")[0]
    assert Path(cdc_a.get_constraints()) == tmp_path / "mod_a.sdc"
    assert Path(cdc_b.get_constraints()) == tmp_path / "mod_b.sdc"
    assert Path(cdc_b.get_waivers()) == tmp_path / "mod_b.waivers"
    assert cdc_a.get_waivers() is None


def test_cdc_suite_config_missing_name_raises(tmp_path):
    from rtl_buddy.errors import FatalRtlBuddyError

    suite_yaml = _write_suite(tmp_path)
    cfg = CdcSuiteConfig(str(suite_yaml))
    with pytest.raises(FatalRtlBuddyError, match="not found"):
        cfg.get_analyses("nonexistent")


def test_cdc_suite_config_duplicate_analysis_raises(tmp_path):
    """Two analyses with the same name in one cdc.yaml is a hard
    error — the dict-comprehension in CdcSuiteConfig.__init__
    would silently overwrite the first with the second otherwise."""
    from rtl_buddy.errors import FatalRtlBuddyError

    (tmp_path / "models.yaml").write_text(_MODELS_YAML)
    body = dedent("""\
        rtl-buddy-filetype: cdc_config

        analyses:
          - name: "dup"
            desc: "first"
            model: "mod_a"
            model_path: "models.yaml"
            tool: "rtl-buddy-cdc"
            constraints: "mod_a.sdc"
            reglvl: 0
          - name: "dup"
            desc: "second"
            model: "mod_b"
            model_path: "models.yaml"
            tool: "rtl-buddy-cdc"
            constraints: "mod_b.sdc"
            reglvl: 0
    """)
    path = tmp_path / "cdc.yaml"
    path.write_text(body)
    with pytest.raises(FatalRtlBuddyError, match="duplicate analysis name 'dup'"):
        CdcSuiteConfig(str(path))


def test_cdc_suite_config_picks_up_frontend_field(tmp_path):
    """Per-analysis `frontend:` round-trips through CdcConfigFile -> CdcConfig."""
    suite_yaml = _write_suite(tmp_path)
    cfg = CdcSuiteConfig(str(suite_yaml))
    cdc_a = cfg.get_analyses("cdc_a")[0]
    cdc_b = cfg.get_analyses("cdc_b")[0]
    assert cdc_a.frontend is None  # not set in YAML -> default
    assert cdc_b.frontend == "slang"  # explicit in YAML


# ---------------------------------------------------------------------------
# CdcRegConfig — YAML loading + per-suite path resolution
# ---------------------------------------------------------------------------

_REG_YAML = dedent("""\
    rtl-buddy-filetype: cdc_reg_config

    cdc-configs:
      - "sandbox/cdc.yaml"
""")


def test_cdc_reg_config_loads_suite_paths(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "models.yaml").write_text(_MODELS_YAML)
    suite_yaml = sandbox / "cdc.yaml"
    suite_yaml.write_text(_SUITE_YAML.format(models_path="models.yaml"))

    reg_yaml = tmp_path / "cdc_regression.yaml"
    reg_yaml.write_text(_REG_YAML)

    reg_cfg = CdcRegConfig(name="reg", path=str(reg_yaml))
    suites = reg_cfg.get_suite_configs()
    assert len(suites) == 1
    assert suites[0].get_analysis_names() == ["cdc_a", "cdc_b"]


# ---------------------------------------------------------------------------
# RtlBuddyCdc — frontend argv plumbing
# ---------------------------------------------------------------------------


def _setup_lint_run(tmp_path, frontend=None):
    """Materialise the minimum on-disk inputs RtlBuddyCdc.run() needs and
    build a ready-to-call wrapper. Returns (wrapper, cmd_calls_list).

    The subprocess is mocked: each invocation is appended to the returned
    list, and the mock writes a minimal valid JSON report so run() can
    finish parsing its output. Use the captured argv to assert on the
    --frontend plumbing.
    """
    from contextlib import nullcontext
    from rtl_buddy.config.cdc import CdcToolConfig, CdcToolConfigFile, CdcToolOptsFile
    from rtl_buddy.config.model import ModelConfig
    from rtl_buddy.process_utils import ManagedProcessResult
    from rtl_buddy.tools import cdc_rtl_buddy as cdc_rtl_buddy_module
    from rtl_buddy.tools.cdc_rtl_buddy import RtlBuddyCdc

    sv = tmp_path / "top.sv"
    sv.write_text("module my_module(); endmodule")
    sdc = tmp_path / "my_module.sdc"
    sdc.write_text("# empty SDC")

    model = ModelConfig(name="my_module", filelist=[f"-v {sv}"], path=str(tmp_path))
    cdc_cfg = CdcConfig(
        name="test_cdc",
        desc="t",
        model=model,
        tool="rtl-buddy-cdc",
        constraints=str(sdc),
        waivers=None,
        _reglvl=None,
        tool_overrides=None,
        frontend=frontend,
    )
    tool_cfg = CdcToolConfig(
        CdcToolConfigFile(
            name="rtl-buddy-cdc",
            tool="rtl-buddy-cdc",
            opts=CdcToolOptsFile(),
        )
    )

    wrapper = RtlBuddyCdc(
        name="t", cdc_cfg=cdc_cfg, tool_cfg=tool_cfg, suite_dir=str(tmp_path)
    )
    json_report = Path(wrapper.artefact_dir) / "cdc.json"

    calls: list[list[str]] = []

    def _fake_run(cmd, stdout, stderr, **kwargs):
        calls.append(list(cmd))
        # Subprocess succeeded; write the minimal payload run() expects so
        # downstream parsing finishes cleanly.
        json_report.write_text('{"summary": {"violations": 0, "suppressed": 0}}')
        return ManagedProcessResult(returncode=0)

    return wrapper, calls, _fake_run, cdc_rtl_buddy_module, nullcontext


def test_lint_argv_omits_frontend_when_unset(tmp_path, monkeypatch):
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path, frontend=None)
    monkeypatch.setattr(mod, "task_status", lambda *a, **kw: nullctx())
    monkeypatch.setattr(mod, "run_managed_process", fake_run)

    wrapper.run()

    assert len(calls) == 2  # text + json
    for cmd in calls:
        assert "--frontend" not in cmd


def test_lint_argv_adds_frontend_slang(tmp_path, monkeypatch):
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path, frontend="slang")
    monkeypatch.setattr(mod, "task_status", lambda *a, **kw: nullctx())
    monkeypatch.setattr(mod, "run_managed_process", fake_run)

    wrapper.run()

    assert len(calls) == 2
    for cmd in calls:
        assert "--frontend" in cmd
        assert cmd[cmd.index("--frontend") + 1] == "slang"


def test_lint_argv_adds_frontend_yosys_when_explicit(tmp_path, monkeypatch):
    """An explicit `frontend: "yosys"` is forwarded as well — useful for
    pinning a config to a specific frontend independent of the tool's own
    default, and as a regression guard against future default changes."""
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path, frontend="yosys")
    monkeypatch.setattr(mod, "task_status", lambda *a, **kw: nullctx())
    monkeypatch.setattr(mod, "run_managed_process", fake_run)

    wrapper.run()

    assert len(calls) == 2
    for cmd in calls:
        assert cmd[cmd.index("--frontend") + 1] == "yosys"


def test_cdc_suite_config_loads_xfail_flags(tmp_path):
    (tmp_path / "models.yaml").write_text(_MODELS_YAML)
    (tmp_path / "cdc.yaml").write_text(
        dedent("""\
        rtl-buddy-filetype: cdc_config

        analyses:
          - name: "cdc_xfail"
            desc: "known violations, non-strict"
            model: "mod_a"
            model_path: "models.yaml"
            tool: "rtl-buddy-cdc"
            constraints: "mod_a.sdc"
            xfail: true
          - name: "cdc_xfail_strict"
            desc: "known violations, strict"
            model: "mod_a"
            model_path: "models.yaml"
            tool: "rtl-buddy-cdc"
            constraints: "mod_a.sdc"
            xfail_strict: true
          - name: "cdc_normal"
            desc: "normal"
            model: "mod_a"
            model_path: "models.yaml"
            tool: "rtl-buddy-cdc"
            constraints: "mod_a.sdc"
    """)
    )
    cfg = CdcSuiteConfig(str(tmp_path / "cdc.yaml"))
    assert cfg.get_analyses("cdc_xfail")[0].is_xfail() is True
    assert cfg.get_analyses("cdc_xfail")[0].get_xfail_strict() is False
    assert cfg.get_analyses("cdc_xfail_strict")[0].is_xfail() is True
    assert cfg.get_analyses("cdc_xfail_strict")[0].get_xfail_strict() is True
    assert cfg.get_analyses("cdc_normal")[0].is_xfail() is False
