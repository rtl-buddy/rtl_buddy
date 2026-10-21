"""Threshold-voltage policy keys: `keep-sizing-vt` and the CTS-only `cts-dont-use-cells` (rtl_buddy#918)."""

import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_pnr_checkpoints import _backend, _pdk, _pnr_cfg, _runs
from test_pnr_run_overrides import _digest_cfg, _platform, _resolved, _run

from rtl_buddy.config.pnr import CHECKPOINT_STAGES
from rtl_buddy.config.pnr_platform import PnrRunOverrides
from rtl_buddy.config.pnr_repair import PnrMode
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_abstract, pnr_checkpoints, pnr_stages
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

_KEEP_VT = "set_opt_config -keep_sizing_vt true\n"
_PLATFORM_DONT_USE = "set_dont_use [list AND2_X1 *_X32]\n"
_CTS_DONT_USE = (
    'puts ">>> CTS don\'t-use cells"\n'
    "with_output_to_variable rb_cts_du_report { report_dont_use }\n"
    "set rb_cts_du_before [dict create]\n"
    'foreach rb_line [lrange [split $rb_cts_du_report "\\n"] 1 end] {\n'
    "  dict set rb_cts_du_before [string trim $rb_line] 1\n"
    "}\n"
    "set rb_cts_du_added [list]\n"
    "foreach rb_cell [get_lib_cells -quiet [list *_L *_SL]] {\n"
    "  if {![dict exists $rb_cts_du_before [get_name $rb_cell]]} {\n"
    "    lappend rb_cts_du_added $rb_cell\n"
    "  }\n"
    "}\n"
    "set_dont_use [list *_L *_SL]\n"
)
_CTS = "clock_tree_synthesis \\\n"
_RESTORE = (
    'puts ">>> Don\'t-use cells after CTS"\n'
    "if {[llength $rb_cts_du_added] > 0} {\n"
    "  unset_dont_use $rb_cts_du_added\n"
    "}\n"
)

#: Stub OpenROAD commands over a model of Resizer's dont-use set. The cells are `RB_STUB_LIB_CELLS`; `RB_STUB_LIB_DONT_USE` seeds the set as Liberty `dont_use` does. CTS prints the set it sees.
_DONT_USE_STUBS = r"""
array set rb_du {}
proc rb_du_cells {patterns} {
    global env
    set hits [list]
    if {![info exists env(RB_STUB_LIB_CELLS)]} { return $hits }
    foreach cell $env(RB_STUB_LIB_CELLS) {
        foreach pattern $patterns {
            if {[string match $pattern $cell]} { lappend hits $cell; break }
        }
    }
    return $hits
}
if {[info exists env(RB_STUB_LIB_DONT_USE)]} {
    foreach rb_c $env(RB_STUB_LIB_DONT_USE) { set rb_du($rb_c) 1 }
}
proc get_lib_cells {args} { return [rb_du_cells [lindex $args end]] }
proc get_name {obj} { return $obj }
proc set_dont_use {cells} { foreach c [rb_du_cells $cells] { set ::rb_du($c) 1 } }
proc unset_dont_use {cells} {
    foreach c [rb_du_cells $cells] { unset -nocomplain ::rb_du($c) }
}
proc rb_du_report {} {
    set text "Don't Use Cells:\n"
    foreach c [lsort [array names ::rb_du]] { append text "  $c\n" }
    return $text
}
proc report_dont_use {} { puts -nonewline [rb_du_report] }
proc with_output_to_variable {var body} {
    upvar 1 $var out
    if {[string trim $body] ne "report_dont_use"} { error "stub: cannot capture $body" }
    set out [rb_du_report]
}
proc clock_tree_synthesis {args} {
    puts "CTS sees dont-use: [lsort [array names ::rb_du]]"
}
"""


def _with_dont_use_stubs(tmp_path) -> None:
    harness = tmp_path / "harness.tcl"
    harness.write_text(
        harness.read_text().replace(
            "source [lindex $argv 0]", _DONT_USE_STUBS + "source [lindex $argv 0]"
        )
    )


#: The placeholders this policy added to the flow template.
_NEW_PLACEHOLDERS = (
    "{{ keep_sizing_vt_block }}",
    "{{ cts_dont_use_block }}",
    "{{ cts_dont_use_restore_block }}",
)


def _vt_platform(tmp_path, *, dont_use=("AND2_X1", "*_X32"), **kw):
    return _platform(
        _pdk(tmp_path, dont_use_cells=list(dont_use)),
        keep_sizing_vt=True,
        cts_dont_use_cells=["*_L", "*_SL"],
        **kw,
    )


def _render(tmp_path, platform, *, mode=PnrMode.CLOSURE, hooks=None) -> str:
    """The one-session `pnr.tcl` of `platform`."""
    pnr_cfg = replace(_pnr_cfg(tmp_path, None), mode=mode, hooks=hooks or {})
    pnr_cfg.resolve_synth_cfg = MagicMock(
        return_value=MagicMock(get_top=lambda: "demo_top", get_name=lambda: "s")
    )
    backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), MagicMock())
    backend._ps_per_unit = 1.0
    return Path(backend._write_script(platform, pnr_cfg.get_floorplan())).read_text()


# -- configuration -----------------------------------------------------------------


def test_both_keys_default_to_off(tmp_path):
    platform = _platform(_pdk(tmp_path))
    assert platform.get_keep_sizing_vt() is False
    assert platform.get_cts_dont_use_cells() == []


@pytest.mark.parametrize(
    ("platform_kw", "run_keys", "getter", "expected"),
    [
        ({}, {"keep-sizing-vt": True}, "get_keep_sizing_vt", True),
        (
            {"keep_sizing_vt": True},
            {"keep-sizing-vt": False},
            "get_keep_sizing_vt",
            False,
        ),
        ({"keep_sizing_vt": True}, {}, "get_keep_sizing_vt", True),
        ({}, {"cts-dont-use-cells": ["*_L"]}, "get_cts_dont_use_cells", ["*_L"]),
        (
            {"cts_dont_use_cells": ["*_SL"]},
            {"cts-dont-use-cells": ["*_L", "*_L"]},
            "get_cts_dont_use_cells",
            ["*_L"],
        ),
        (
            {"cts_dont_use_cells": ["*_SL"]},
            {"cts-dont-use-cells": []},
            "get_cts_dont_use_cells",
            [],
        ),
        ({"cts_dont_use_cells": ["*_SL"]}, {}, "get_cts_dont_use_cells", ["*_SL"]),
    ],
)
def test_a_run_value_wins_over_the_platform(
    tmp_path, platform_kw, run_keys, getter, expected
):
    platform = _platform(_pdk(tmp_path), **platform_kw)
    before = getattr(platform, getter)()

    resolved = _resolved(tmp_path, platform, **run_keys)

    assert getattr(resolved, getter)() == expected
    assert getattr(platform, getter)() == before


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("*_L", "cts-dont-use-cells must be a list of cell-name patterns"),
        (["a b"], "cts-dont-use-cells entry 'a b' contains whitespace"),
        ([""], "cts-dont-use-cells entries must be non-empty"),
        (["[x]"], "cts-dont-use-cells entry '[x]' contains"),
    ],
)
def test_a_bad_cts_dont_use_list_is_refused_naming_the_key(tmp_path, value, message):
    with pytest.raises(FatalRtlBuddyError, match=r"pnr run 'r': ") as run_exc:
        _run(tmp_path, **{"cts-dont-use-cells": value})
    assert message in str(run_exc.value)
    with pytest.raises(FatalRtlBuddyError, match=r"pnr platform 'pl': ") as pl_exc:
        _platform(_pdk(tmp_path), cts_dont_use_cells=value)
    assert message in str(pl_exc.value)


# -- the one-session flow -----------------------------------------------------------


def test_keep_sizing_vt_follows_the_flow_s_set_dont_use(tmp_path):
    session = tmp_path / "session.tcl"
    session.write_text("# hook\n")
    text = _render(tmp_path, _vt_platform(tmp_path), hooks={"session": (str(session),)})

    assert text.count(_KEEP_VT) == 1
    assert _PLATFORM_DONT_USE + _KEEP_VT in text
    assert text.index(_KEEP_VT) < text.index("rb::hook::run session")
    assert text.index(_KEEP_VT) < text.index("initialize_floorplan")


def test_keep_sizing_vt_without_dont_use_cells_follows_the_sdc(tmp_path):
    text = _render(tmp_path, _vt_platform(tmp_path, dont_use=()))

    assert "read_sdc $SDC_FILE\n" + _KEEP_VT in text


def test_the_cts_list_brackets_cts_and_repair_clock_nets(tmp_path):
    hook = tmp_path / "after_cts.tcl"
    hook.write_text("# hook\n")
    text = _render(
        tmp_path,
        _vt_platform(tmp_path, cts_repair_clock_nets=True),
        hooks={"after-cts": (str(hook),)},
    )

    assert text.count(_CTS_DONT_USE) == 1
    assert _CTS_DONT_USE + _CTS in text
    restore = _RESTORE
    assert text.count(restore) == 1
    assert "repair_clock_nets\n" + restore in text
    order = [
        text.index(_KEEP_VT),
        text.index(_CTS_DONT_USE),
        text.index(_CTS),
        text.index("repair_clock_nets\n"),
        text.index(restore),
        text.index("rb::hook::run after-cts"),
        text.index('puts ">>> Post-CTS timing repair"'),
        text.index("repair_timing -hold"),
    ]
    assert order == sorted(order)


def test_the_restore_follows_cts_when_repair_clock_nets_is_off(tmp_path):
    text = _render(tmp_path, _vt_platform(tmp_path))

    restore = _RESTORE
    assert "repair_clock_nets" not in text
    cts_at = text.index(_CTS)
    assert (
        cts_at < text.index(restore) < text.index('puts ">>> Post-CTS timing repair"')
    )


def test_the_restore_unsets_only_what_the_cts_list_added(tmp_path):
    """The snapshot of the dont-use set precedes the exclusion; the restore names no pattern and re-applies no list."""
    text = _render(tmp_path, _vt_platform(tmp_path, cts_repair_clock_nets=True))

    exclusion = text[text.index('puts ">>> CTS don\'t-use cells"') : text.index(_CTS)]
    assert exclusion.index("report_dont_use") < exclusion.index(
        "get_lib_cells -quiet [list *_L *_SL]"
    )
    assert exclusion.index("get_lib_cells") < exclusion.index(
        "set_dont_use [list *_L *_SL]"
    )
    assert "unset_dont_use [list" not in text
    assert text.count("unset_dont_use") == 1
    assert text.count(_PLATFORM_DONT_USE) == 1


def test_the_run_resolved_cts_list_brackets_cts(tmp_path):
    platform = _resolved(
        tmp_path,
        _platform(
            _pdk(tmp_path, dont_use_cells=["AND2_X1"]),
            dont_use_cells=["*_X32"],
            cts_dont_use_cells=["*_SL"],
        ),
        **{"cts-dont-use-cells": ["*_L"]},
    )
    text = _render(tmp_path, platform)

    assert _CTS_DONT_USE.replace("[list *_L *_SL]", "[list *_L]") + _CTS in text
    assert text.count(_RESTORE) == 1
    assert text.count("set_dont_use [list AND2_X1 *_X32]\n") == 1
    assert "*_SL" not in text


def test_the_cts_list_stays_out_of_the_routed_design_check(tmp_path):
    text = _render(tmp_path, _vt_platform(tmp_path))

    check = text[text.index('puts ">>> Don\'t-use check"') :]
    assert "foreach rb_pattern [list AND2_X1 *_X32] {" in check
    assert "*_L" not in check


def test_a_probe_brackets_its_cts_too(tmp_path):
    text = _render(tmp_path, _vt_platform(tmp_path), mode=PnrMode.PROBE)

    assert _CTS_DONT_USE + _CTS in text
    assert _RESTORE in text
    assert text.count(_KEEP_VT) == 1


# -- staged and resumed runs ---------------------------------------------------------


def _staged_scripts(tmp_path, platform) -> dict[str, str]:
    """Every session script of a staged run with post-route repair, by name."""
    pnr_cfg = _pnr_cfg(tmp_path, stages=True)
    pnr_cfg.resolve_synth_cfg = MagicMock(
        return_value=MagicMock(get_top=lambda: "demo_top", get_name=lambda: "s")
    )
    backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), MagicMock())
    backend._ps_per_unit = 1.0
    run_dir = str(tmp_path / "ck" / "run")
    backend._ckpt_run_dir = run_dir
    os.makedirs(run_dir, exist_ok=True)
    backend._write_script(platform, pnr_cfg.get_floorplan())
    scripts = {p.name: p.read_text() for p in sorted(Path(run_dir).glob("*.tcl"))}
    scripts["round 0 extraction"] = backend._post_route_extract_script(0, run_dir)
    scripts["round 1 repair"] = backend._post_route_repair_script(1, run_dir)
    scripts["round report"] = backend._post_route_report_script(run_dir)
    return scripts


def _stage_file(stage: str) -> str:
    return f"{pnr_checkpoints.STAGE_INDEX[stage]}_{stage}.tcl"


def test_every_staged_session_sets_keep_sizing_vt_after_its_dont_use(tmp_path):
    platform = _vt_platform(tmp_path, post_route_repair={"hold-margin": 0, "rounds": 1})
    scripts = _staged_scripts(tmp_path, platform)

    expected = {_stage_file(s) for s in pnr_stages.TEMPLATE_STAGES} | {
        "round 0 extraction",
        "round 1 repair",
        "round report",
    }
    assert expected <= set(scripts)
    for name in expected:
        text = scripts[name]
        assert text.count(_KEEP_VT) == 1, name
        assert _PLATFORM_DONT_USE + _KEEP_VT in text, name


def test_only_the_cts_session_brackets_cts(tmp_path):
    platform = _vt_platform(
        tmp_path,
        cts_repair_clock_nets=True,
        post_route_repair={"hold-margin": 0, "rounds": 1},
    )
    scripts = _staged_scripts(tmp_path, platform)

    holders = sorted(n for n, t in scripts.items() if "*_L" in t)
    assert holders == [_stage_file("cts")]
    cts = scripts[_stage_file("cts")]
    assert cts.count(_CTS_DONT_USE) == 1
    assert _CTS_DONT_USE + _CTS in cts
    assert "repair_clock_nets\n" + _RESTORE in cts
    # The prelude's own `set_dont_use` precedes the CTS exclusion.
    assert cts.index(_PLATFORM_DONT_USE) < cts.index(_CTS_DONT_USE)


def test_a_run_resumed_at_cts_brackets_cts_in_its_session(tmp_path, monkeypatch):
    first, artefacts = _backend(tmp_path, monkeypatch, stages=True, stop_after="place")
    _with_dont_use_stubs(tmp_path)
    first.root_cfg.get_pnr_platform_cfg.return_value = _vt_platform(tmp_path)
    assert first.run().results["result"] == "PASS"
    (stopped,) = _runs(artefacts)

    resumed, _ = _backend(tmp_path, monkeypatch, resume=stopped.name, resume_from="cts")
    _with_dont_use_stubs(tmp_path)
    resumed.root_cfg.get_pnr_platform_cfg.return_value = _vt_platform(tmp_path)
    res = resumed.run()

    assert res.results["result"] == "PASS", res.results
    latest = max(_runs(artefacts), key=lambda p: p.stat().st_mtime)
    cts = (latest / _stage_file("cts")).read_text()
    assert _CTS_DONT_USE + _CTS in cts
    assert _RESTORE in cts
    for stage in ("cts", "global_route", "detailed_route", "finish"):
        assert (latest / _stage_file(stage)).read_text().count(_KEEP_VT) == 1, stage


@pytest.mark.parametrize("stages", [False, True], ids=["one session", "stages"])
def test_cts_frees_only_the_cells_its_list_made_dont_use(tmp_path, monkeypatch, stages):
    """Liberty's, the platform's and a hook's dont-use cells that match the CTS list stay dont-use after CTS."""
    session = tmp_path / "session.tcl"
    session.write_text("set_dont_use HOOK_L\n")
    after_cts = tmp_path / "after_cts.tcl"
    after_cts.write_text("report_dont_use\n")
    backend, artefacts = _backend(
        tmp_path,
        monkeypatch,
        checkpoints=None,
        stages=stages,
        env={
            "RB_STUB_LIB_CELLS": "BUF_R CTS_L CTS2_L LIBDU_L HOOK_L PLAT_L",
            "RB_STUB_LIB_DONT_USE": "LIBDU_L",
        },
    )
    _with_dont_use_stubs(tmp_path)
    backend.root_cfg.get_pnr_platform_cfg.return_value = _platform(
        _pdk(tmp_path, dont_use_cells=["PLAT_L"]),
        cts_dont_use_cells=["*_L"],
        cts_repair_clock_nets=True,
    )
    backend.pnr_cfg.hooks = {"session": (str(session),), "after-cts": (str(after_cts),)}

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    log = "\n".join(
        p.read_text() for p in sorted(artefacts.rglob("*.log")) if "pnr" in p.name
    )
    assert "CTS sees dont-use: CTS2_L CTS_L HOOK_L LIBDU_L PLAT_L\n" in log
    after = log[log.index(">>> Hook after-cts:") :]
    assert after.split("Don't Use Cells:\n", 1)[1].startswith(
        "  HOOK_L\n  LIBDU_L\n  PLAT_L\n"
    )


# -- unset ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["one session", "checkpoints", "stages"])
def test_unset_keys_leave_every_script_as_before(tmp_path, monkeypatch, kind):
    """With both keys unset, every script a run writes is the render of the template without this policy's placeholders."""
    platform = _platform(
        _pdk(tmp_path, dont_use_cells=["AND2_X1"]),
        cts_repair_clock_nets=True,
        keep_sizing_vt=False,
        cts_dont_use_cells=[],
    )

    def _write(base: bool) -> dict[str, str]:
        pnr_cfg = _pnr_cfg(
            tmp_path,
            CHECKPOINT_STAGES if kind == "checkpoints" else None,
            stages=kind == "stages",
        )
        pnr_cfg.resolve_synth_cfg = MagicMock(
            return_value=MagicMock(get_top=lambda: "demo_top", get_name=lambda: "s")
        )
        backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), MagicMock())
        backend._ps_per_unit = 1.0
        if kind != "one session":
            backend._ckpt_run_dir = str(tmp_path / "ck" / "run")
            os.makedirs(backend._ckpt_run_dir, exist_ok=True)
        if base:
            template = backend._load_template()
            for placeholder in _NEW_PLACEHOLDERS:
                assert template.count(placeholder) == 1
                template = template.replace(placeholder, "")
            monkeypatch.setattr(backend, "_load_template", lambda: template)
        script = Path(backend._write_script(platform, pnr_cfg.get_floorplan()))
        written = {"pnr.tcl": script.read_text()}
        if backend._ckpt_run_dir:
            for path in sorted(Path(backend._ckpt_run_dir).glob("*.tcl")):
                written[path.name] = path.read_text()
        return written

    now = _write(base=False)

    assert now == _write(base=True)
    assert "set_opt_config" not in "".join(now.values())
    assert "unset_dont_use" not in "".join(now.values())


# -- configuration digest ------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "key", "value"),
    [
        (PnrRunOverrides(keep_sizing_vt=True), "keep_sizing_vt", True),
        (PnrRunOverrides(cts_dont_use_cells=("*_L",)), "cts_dont_use_cells", ["*_L"]),
    ],
)
def test_a_set_key_joins_the_digest(tmp_path, overrides, key, value):
    platform = _platform(_pdk(tmp_path))
    plain = pnr_abstract.abstract_config(_digest_cfg(tmp_path), platform)
    over = pnr_abstract.abstract_config(_digest_cfg(tmp_path, overrides), platform)

    assert key not in plain
    assert over[key] == value
    assert pnr_abstract.config_digest(over) != pnr_abstract.config_digest(plain)


def test_a_platform_key_joins_the_digest(tmp_path):
    config = pnr_abstract.abstract_config(_digest_cfg(tmp_path), _vt_platform(tmp_path))
    assert config["keep_sizing_vt"] is True
    assert config["cts_dont_use_cells"] == ["*_L", "*_SL"]


def test_explicit_off_values_digest_as_unset_ones(tmp_path):
    platform = _platform(_pdk(tmp_path))
    plain = pnr_abstract.abstract_config(_digest_cfg(tmp_path), platform)
    explicit = pnr_abstract.abstract_config(
        _digest_cfg(
            tmp_path, PnrRunOverrides(keep_sizing_vt=False, cts_dont_use_cells=())
        ),
        platform,
    )
    assert pnr_abstract.config_digest(explicit) == pnr_abstract.config_digest(plain)


def test_the_pnr_yaml_keys_reach_the_digest(tmp_path):
    (tmp_path / "synth.yaml").write_text(
        "rtl-buddy-filetype: synth_config\nsyntheses: []\n"
    )
    run = _run(
        tmp_path,
        harden=True,
        **{"keep-sizing-vt": True, "cts-dont-use-cells": ["*_L"]},
    )
    config = pnr_abstract.abstract_config(run, _platform(_pdk(tmp_path)))
    assert config["keep_sizing_vt"] is True
    assert config["cts_dont_use_cells"] == ["*_L"]
