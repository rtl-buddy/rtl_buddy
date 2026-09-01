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
    single_unit=False,
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
        single_unit=single_unit,
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


def test_cdc_config_single_unit_defaults_to_false():
    cfg = _make_cdc_cfg()
    assert cfg.single_unit is False


def test_cdc_config_single_unit_explicit_true():
    cfg = _make_cdc_cfg(single_unit=True)
    assert cfg.single_unit is True


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
        single_unit: true
        blackbox: ["sram_macro", "pll_wrap"]
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


def test_cdc_suite_config_picks_up_single_unit_field(tmp_path):
    suite_yaml = _write_suite(tmp_path)
    cfg = CdcSuiteConfig(str(suite_yaml))
    cdc_a = cfg.get_analyses("cdc_a")[0]
    cdc_b = cfg.get_analyses("cdc_b")[0]
    assert cdc_a.single_unit is False
    assert cdc_b.single_unit is True


def test_cdc_suite_config_picks_up_blackbox_field(tmp_path):
    """Per-analysis `blackbox:` round-trips through CdcConfigFile -> CdcConfig."""
    suite_yaml = _write_suite(tmp_path)
    cfg = CdcSuiteConfig(str(suite_yaml))
    cdc_a = cfg.get_analyses("cdc_a")[0]
    cdc_b = cfg.get_analyses("cdc_b")[0]
    assert cdc_a.blackbox == []  # not set in YAML -> default empty list
    assert cdc_b.blackbox == ["sram_macro", "pll_wrap"]  # explicit in YAML


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
    assert suites[0].get_analyses("cdc_b")[0].single_unit is True


# ---------------------------------------------------------------------------
# RtlBuddyCdc — frontend argv plumbing
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _analyzer_on_path(monkeypatch):
    """Pretend rtl-buddy-cdc is installed.

    `RtlBuddyCdc.run` skips when the analyzer is not on PATH (#469), and
    rtl_buddy does not depend on it — so without this the argv tests below
    would pass on a developer box that happens to have it and skip-and-fail
    in CI. Tests that care about the analyzer being absent re-patch `which`
    themselves; the later `setattr` wins.
    """
    from rtl_buddy.tools import cdc_rtl_buddy as _mod

    monkeypatch.setattr(_mod.shutil, "which", lambda name: f"/fake/bin/{name}")


def _setup_lint_run(
    tmp_path, frontend=None, single_unit=False, blackbox=None, emit_maps=False
):
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
        single_unit=single_unit,
        blackbox=blackbox if blackbox is not None else [],
    )
    tool_cfg = CdcToolConfig(
        CdcToolConfigFile(
            name="rtl-buddy-cdc",
            tool="rtl-buddy-cdc",
            opts=CdcToolOptsFile(),
        )
    )

    wrapper = RtlBuddyCdc(
        name="t",
        cdc_cfg=cdc_cfg,
        tool_cfg=tool_cfg,
        suite_dir=str(tmp_path),
        emit_maps=emit_maps,
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
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)

    wrapper.run()

    assert len(calls) == 2  # text + json
    for cmd in calls:
        assert "--frontend" not in cmd


def test_lint_argv_adds_frontend_slang(tmp_path, monkeypatch):
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path, frontend="slang")
    monkeypatch.setattr(mod, "task_status", lambda *a, **kw: nullctx())
    monkeypatch.setattr(mod, "run_managed_process", fake_run)
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)

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
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)

    wrapper.run()

    assert len(calls) == 2
    for cmd in calls:
        assert cmd[cmd.index("--frontend") + 1] == "yosys"


def test_lint_argv_omits_single_unit_when_unset(tmp_path, monkeypatch):
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path)
    monkeypatch.setattr(mod, "task_status", lambda *a, **kw: nullctx())
    monkeypatch.setattr(mod, "run_managed_process", fake_run)
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)

    wrapper.run()

    assert len(calls) == 2
    for cmd in calls:
        assert "--single-unit" not in cmd


def test_lint_argv_adds_single_unit_to_both_reports(tmp_path, monkeypatch):
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(
        tmp_path, frontend="yosys", single_unit=True
    )
    monkeypatch.setattr(mod, "task_status", lambda *a, **kw: nullctx())
    monkeypatch.setattr(mod, "run_managed_process", fake_run)
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)
    monkeypatch.setattr(mod, "_lint_supports_single_unit", lambda exe: True)

    wrapper.run()

    assert len(calls) == 2
    for cmd in calls:
        assert cmd.count("--single-unit") == 1


def test_lint_single_unit_requires_analyzer_support(tmp_path, monkeypatch):
    from rtl_buddy.errors import FatalRtlBuddyError

    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path, single_unit=True)
    monkeypatch.setattr(mod, "task_status", lambda *a, **kw: nullctx())
    monkeypatch.setattr(mod, "run_managed_process", fake_run)
    monkeypatch.setattr(mod, "_lint_supports_single_unit", lambda exe: False)

    with pytest.raises(FatalRtlBuddyError, match="single_unit: true.*#277"):
        wrapper.run()

    assert calls == []


# ---------------------------------------------------------------------------
# RtlBuddyCdc — --blackbox argv plumbing (rtl-buddy-cdc#259)
# ---------------------------------------------------------------------------


def test_lint_argv_omits_blackbox_when_empty(tmp_path, monkeypatch):
    """An empty/absent blackbox list adds no `--blackbox` args."""
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path, blackbox=[])
    monkeypatch.setattr(mod, "task_status", lambda *a, **kw: nullctx())
    monkeypatch.setattr(mod, "run_managed_process", fake_run)
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)

    wrapper.run()

    assert len(calls) == 2  # text + json
    for cmd in calls:
        assert "--blackbox" not in cmd


def test_lint_argv_adds_blackbox_for_each_module(tmp_path, monkeypatch):
    """Each blackbox entry is forwarded as a repeated `--blackbox <module>`."""
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(
        tmp_path, blackbox=["foo", "bar"]
    )
    monkeypatch.setattr(mod, "task_status", lambda *a, **kw: nullctx())
    monkeypatch.setattr(mod, "run_managed_process", fake_run)
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)

    wrapper.run()

    assert len(calls) == 2
    for cmd in calls:
        # Both modules present, each preceded by its own `--blackbox`.
        bb_values = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--blackbox"]
        assert bb_values == ["foo", "bar"]
        assert cmd[cmd.index("--blackbox") + 1] == "foo"


# ---------------------------------------------------------------------------
# RtlBuddyCdc — --project-root plumbing (rtl-buddy-cdc#245)
# ---------------------------------------------------------------------------


def test_lint_argv_adds_project_root_when_supported(tmp_path, monkeypatch):
    """When the analyzer advertises `--project-root`, both invocations get
    `--project-root <suite_dir>` so a config's relative `extra_args` paths
    resolve against the cdc.yaml dir rather than the nested artefact cwd."""
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path)
    monkeypatch.setattr(mod, "task_status", lambda *a, **kw: nullctx())
    monkeypatch.setattr(mod, "run_managed_process", fake_run)
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: True)

    wrapper.run()

    assert len(calls) == 2
    for cmd in calls:
        assert "--project-root" in cmd
        assert cmd[cmd.index("--project-root") + 1] == str(tmp_path)


def test_lint_argv_omits_project_root_when_unsupported(tmp_path, monkeypatch):
    """An analyzer that predates the flag must not be handed it — passing an
    unknown option would hard-fail (exit 2). Degrade silently instead."""
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path)
    monkeypatch.setattr(mod, "task_status", lambda *a, **kw: nullctx())
    monkeypatch.setattr(mod, "run_managed_process", fake_run)
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)

    wrapper.run()

    assert len(calls) == 2
    for cmd in calls:
        assert "--project-root" not in cmd


def test_lint_argv_project_root_precedes_extra_args(tmp_path, monkeypatch):
    """`--project-root` is emitted before `extra_args` so a config can still
    override the anchor in its own `extra_args` if it ever needs to."""
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path)
    # Inject a path-bearing extra_arg of the kind #245 is about.
    wrapper.tool_cfg._cfg.opts.extra_args = "--yosys-plugin build/slang.so"
    monkeypatch.setattr(mod, "task_status", lambda *a, **kw: nullctx())
    monkeypatch.setattr(mod, "run_managed_process", fake_run)
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: True)

    wrapper.run()

    for cmd in calls:
        assert cmd.index("--project-root") < cmd.index("--yosys-plugin")


def test_lint_supports_project_root_probe(monkeypatch):
    """The capability probe greps `lint --help` and degrades to False on a
    missing/erroring binary (so the cache never sticks a flag onto an old
    analyzer)."""
    from types import SimpleNamespace
    from rtl_buddy.tools import cdc_rtl_buddy as mod

    mod._lint_supports_project_root.cache_clear()

    def _help_with_flag(cmd, **kwargs):
        return SimpleNamespace(stdout="... --project-root DIR ...", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", _help_with_flag)
    assert mod._lint_supports_project_root("cdc-new") is True

    def _boom(cmd, **kwargs):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(mod.subprocess, "run", _boom)
    assert mod._lint_supports_project_root("cdc-missing") is False
    mod._lint_supports_project_root.cache_clear()


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


# ---------------------------------------------------------------------------
# RtlBuddyCdc — stale-report masking (#469)
# ---------------------------------------------------------------------------


def test_lint_stale_json_report_is_not_reported_as_this_run(tmp_path, monkeypatch):
    """A crash that exits 1 without writing a report must not resurrect the
    previous run's cdc.json (#469).

    Exit code 1 is the analyzer's "rule violations found" code, so a crash
    that happens to exit 1 passes the returncode gate. Before the fix the
    stale report left in the artefact dir was parsed and its counts were
    reported as the current result.
    """
    wrapper, calls, _fake_run, mod, nullctx = _setup_lint_run(tmp_path)
    monkeypatch.setattr(mod, "task_status", lambda *a, **k: nullctx())
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)

    stale = Path(wrapper.artefact_dir) / "cdc.json"
    stale.write_text('{"summary": {"violations": 31, "crossings": 49}}')
    stale_txt = Path(wrapper.artefact_dir) / "cdc.txt"
    stale_txt.write_text("31 violations from a previous, unrelated run\n")

    from rtl_buddy.process_utils import ManagedProcessResult

    def _crash(cmd, stdout, stderr, **kwargs):
        # Exits with the "violations found" code but writes no report.
        return ManagedProcessResult(returncode=1)

    monkeypatch.setattr(mod, "run_managed_process", _crash)

    res = wrapper.run()

    assert res.results["violations"] == 0
    assert "no JSON report produced" in res.results["desc"]
    assert res.is_pass() is False
    assert not stale.exists()
    assert not stale_txt.exists()


def test_lint_stale_domain_maps_are_cleared_when_emitting(tmp_path, monkeypatch):
    """`--emit-constraints` reads domain_map.json / reset_map.json back off
    disk; a crashed run must not hand it the previous run's maps (#469)."""
    wrapper, calls, _fake_run, mod, nullctx = _setup_lint_run(tmp_path, emit_maps=True)
    monkeypatch.setattr(mod, "task_status", lambda *a, **k: nullctx())
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)

    domain_map = Path(wrapper.artefact_dir) / "domain_map.json"
    reset_map = Path(wrapper.artefact_dir) / "reset_map.json"
    domain_map.write_text('{"clocks": {"stale_clk": []}}')
    reset_map.write_text('{"resets": {"stale_rst": []}}')

    from rtl_buddy.process_utils import ManagedProcessResult

    def _crash(cmd, stdout, stderr, **kwargs):
        return ManagedProcessResult(returncode=2)

    monkeypatch.setattr(mod, "run_managed_process", _crash)

    res = wrapper.run()

    assert res.results["violations"] == 0
    assert not domain_map.exists()
    assert not reset_map.exists()
    assert wrapper.read_emitted_maps() == (None, None)
    assert wrapper.read_report() == {}


def test_lint_fresh_report_from_this_run_is_still_consumed(tmp_path, monkeypatch):
    """The pre-run cleanup must not break the happy path: a report the
    current invocation writes is parsed normally (#469)."""
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path)
    monkeypatch.setattr(mod, "task_status", lambda *a, **k: nullctx())
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)

    stale = Path(wrapper.artefact_dir) / "cdc.json"
    stale.write_text('{"summary": {"violations": 31, "crossings": 49}}')

    monkeypatch.setattr(mod, "run_managed_process", fake_run)

    res = wrapper.run()

    assert res.results["violations"] == 0
    assert res.is_pass()


def test_lint_missing_analyzer_skips_and_keeps_the_previous_reports(
    tmp_path, monkeypatch
):
    """A box without rtl-buddy-cdc never ran it, so it must not delete the
    reports a box that has it produced — the same carve-out the Vivado
    backend has, and what the docs promise (#469)."""
    from rtl_buddy.runner.cdc_results import CdcSkipResults

    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(mod, "run_managed_process", fake_run)

    kept = Path(wrapper.artefact_dir) / "cdc.json"
    kept.write_text('{"summary": {"violations": 3}}')
    kept_txt = Path(wrapper.artefact_dir) / "cdc.txt"
    kept_txt.write_text("3 violations from the box that has the analyzer\n")

    res = wrapper.run()

    assert isinstance(res, CdcSkipResults)
    assert "not found" in res.results["desc"]
    assert "tool-check" in res.results["desc"]
    # Nothing was run, so nothing is deleted.
    assert calls == []
    assert kept.exists()
    assert kept_txt.exists()


def test_lint_config_error_beats_the_missing_analyzer_skip(tmp_path, monkeypatch):
    """A broken analysis is broken on every machine. Reporting it as "analyzer
    not installed" on a box that merely lacks the tool would send the user
    after the wrong problem, so the config validation runs first (#469)."""
    import os

    from rtl_buddy.errors import FatalRtlBuddyError

    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)

    # The analysis names an SDC that does not exist.
    os.unlink(wrapper.cdc_cfg.get_constraints())

    with pytest.raises(FatalRtlBuddyError, match="SDC not found"):
        wrapper.run()


def test_lint_missing_analyzer_skip_still_fires_for_a_valid_analysis(
    tmp_path, monkeypatch
):
    """The reorder must not cost the skip: a well-configured analysis on a box
    without the analyzer still skips with its reports intact (#469)."""
    from rtl_buddy.runner.cdc_results import CdcSkipResults

    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)

    kept = Path(wrapper.artefact_dir) / "cdc.json"
    kept.write_text('{"summary": {"violations": 3}}')

    res = wrapper.run()

    assert isinstance(res, CdcSkipResults)
    assert kept.exists()


def test_lint_config_failure_clears_the_previous_reports(tmp_path, monkeypatch):
    """A config error is a failed run, so it must not leave the previous
    run's reports to be read as this one's (#469)."""
    import os

    from rtl_buddy.errors import FatalRtlBuddyError

    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path)
    stale = Path(wrapper.artefact_dir) / "cdc.json"
    stale.write_text('{"summary": {"violations": 31}}')
    os.unlink(wrapper.cdc_cfg.get_constraints())

    with pytest.raises(FatalRtlBuddyError, match="SDC not found"):
        wrapper.run()

    assert not stale.exists()


def test_lint_clears_maps_even_when_not_emitting_them(tmp_path, monkeypatch):
    """`--emit-constraints` / `--check-xdc` read the maps back off a fixed
    path, so an ordinary run must not leave an earlier constraint-generation
    run's maps for them to answer from (#469)."""
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path, emit_maps=False)
    monkeypatch.setattr(mod, "task_status", lambda *a, **k: nullctx())
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)
    monkeypatch.setattr(mod, "run_managed_process", fake_run)

    domain_map = Path(wrapper.artefact_dir) / "domain_map.json"
    reset_map = Path(wrapper.artefact_dir) / "reset_map.json"
    domain_map.write_text('{"clocks": ["from an --emit-constraints run"]}')
    reset_map.write_text('{"reset_synchronizers": []}')

    wrapper.run()

    assert not domain_map.exists()
    assert not reset_map.exists()
    assert wrapper.read_emitted_maps() == (None, None)


def test_lint_analyzer_writes_then_fails_publishes_nothing(tmp_path, monkeypatch):
    """The analyzer writes its report before it finishes, so an unsupported
    exit code can arrive with a report on disk. A FAIL publishes nothing, or
    `read_report` hands the CLI a report the run disowned (#469)."""
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path, emit_maps=True)
    monkeypatch.setattr(mod, "task_status", lambda *a, **k: nullctx())
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)

    from rtl_buddy.process_utils import ManagedProcessResult

    report = Path(wrapper.artefact_dir) / "cdc.json"
    domain_map = Path(wrapper.artefact_dir) / "domain_map.json"

    def _writes_then_dies(cmd, stdout, stderr, **kwargs):
        report.write_text('{"summary": {"violations": 7}}')
        domain_map.write_text('{"clocks": []}')
        return ManagedProcessResult(returncode=2)

    monkeypatch.setattr(mod, "run_managed_process", _writes_then_dies)

    res = wrapper.run()

    assert res.results["violations"] == 0
    assert "exited with code 2" in res.results["desc"]
    assert not report.exists()
    assert not domain_map.exists()
    assert wrapper.read_report() == {}


def test_lint_unparsable_report_publishes_nothing(tmp_path, monkeypatch):
    """Same for a report the wrapper cannot parse (#469)."""
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path)
    monkeypatch.setattr(mod, "task_status", lambda *a, **k: nullctx())
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)

    from rtl_buddy.process_utils import ManagedProcessResult

    report = Path(wrapper.artefact_dir) / "cdc.json"

    def _writes_garbage(cmd, stdout, stderr, **kwargs):
        report.write_text("{ not json")
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(mod, "run_managed_process", _writes_garbage)

    res = wrapper.run()

    assert "could not parse JSON report" in res.results["desc"]
    assert not report.exists()


def test_lint_filelist_error_clears_the_previous_reports(tmp_path, monkeypatch):
    """`_write_filelist` raises `FilelistError`, a *sibling* of
    `FatalRtlBuddyError` under `RtlBuddyError` rather than a subclass. A
    rerun after a source file disappears must still publish nothing (#469)."""
    import os

    from rtl_buddy.errors import FatalRtlBuddyError, FilelistError, RtlBuddyError

    # Guard the premise: catching FatalRtlBuddyError alone would miss this.
    assert not issubclass(FilelistError, FatalRtlBuddyError)
    assert issubclass(FilelistError, RtlBuddyError)

    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path)

    # What a previously successful analysis left behind.
    report = Path(wrapper.artefact_dir) / "cdc.json"
    report.write_text('{"summary": {"violations": 31, "crossings": 49}}')
    text = Path(wrapper.artefact_dir) / "cdc.txt"
    text.write_text("31 violations\n")
    domain_map = Path(wrapper.artefact_dir) / "domain_map.json"
    domain_map.write_text('{"clocks": ["clk_a"]}')
    reset_map = Path(wrapper.artefact_dir) / "reset_map.json"
    reset_map.write_text('{"reset_synchronizers": []}')

    # The source named by the model is gone.
    os.unlink(tmp_path / "top.sv")

    with pytest.raises(RtlBuddyError):
        wrapper.run()

    assert not report.exists()
    assert not text.exists()
    assert not domain_map.exists()
    assert not reset_map.exists()
    assert wrapper.read_report() == {}
    assert wrapper.read_emitted_maps() == (None, None)


@pytest.mark.parametrize(
    "payload, why",
    [
        ('[{"summary": {"violations": 0}}]', "top-level list"),
        ('{"summary": []}', "summary is a list"),
        ('{"summary": {"violations": "many"}}', "non-numeric violations"),
        ('{"summary": {"violations": 0, "crossings": {}}}', "non-numeric crossings"),
        ('"just a string"', "top-level string"),
    ],
)
def test_lint_structurally_bad_report_publishes_nothing(
    tmp_path, monkeypatch, payload, why
):
    """`json.loads` succeeding only says the bytes were valid JSON. A
    top-level list or a non-numeric `summary.violations` parses fine and then
    raises on `.get` or `int()` — outside the guard that escaped
    `_fail_after_analyzer`, leaving the rejected report on disk (#469)."""
    wrapper, calls, fake_run, mod, nullctx = _setup_lint_run(tmp_path, emit_maps=True)
    monkeypatch.setattr(mod, "task_status", lambda *a, **k: nullctx())
    monkeypatch.setattr(mod, "_lint_supports_project_root", lambda exe: False)

    from rtl_buddy.process_utils import ManagedProcessResult

    report = Path(wrapper.artefact_dir) / "cdc.json"
    text = Path(wrapper.artefact_dir) / "cdc.txt"
    domain_map = Path(wrapper.artefact_dir) / "domain_map.json"

    def _writes_odd_shape(cmd, stdout, stderr, **kwargs):
        report.write_text(payload)
        text.write_text("a text report\n")
        domain_map.write_text('{"clocks": []}')
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(mod, "run_managed_process", _writes_odd_shape)

    res = wrapper.run()

    assert "could not parse JSON report" in res.results["desc"], why
    assert res.results["violations"] == 0
    assert not report.exists(), why
    assert not text.exists(), why
    assert not domain_map.exists(), why
    assert wrapper.read_report() == {}
