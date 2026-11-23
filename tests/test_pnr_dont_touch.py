"""The `dont-touch:` run key (rtl_buddy#1077): its validation, where the flow marks the instances, the global connections around the power grid, and the config digest.

The Tcl procedures are run under a bare Tcl interpreter against a mock block.
"""

import os
from importlib.resources import files
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tcl_eval import _tcl_eval
from pnr_stub_openroad import _pdk, _platform, _pnr_cfg

from rtl_buddy.config.pnr import CHECKPOINT_STAGES, PnrConfigFile, PnrFloorplanFile
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_abstract
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

_PLACEHOLDERS = (
    "{{ dont_touch_block }}",
    "{{ dont_touch_lift_block }}",
    "{{ dont_touch_restore_block }}",
)
_APPLY = (
    "rb::dont_touch::apply [list [list {dont-touch[0]} {u_blk.u_clk_buf}] "
    "[list {dont-touch[1]} {g_fwd[*].u_buf}]]\n"
)


def _scripts(tmp_path, *, dont_touch=(), kind="one session", template=None):
    """Every script a run writes, by file name; `template` replaces the flow template."""
    pdn = tmp_path / "pdn.tcl"
    pdn.write_text("add_global_connection -net VDD -pin_pattern VDD -power\n")
    hook = tmp_path / "after_pdn.tcl"
    hook.write_text("")
    pnr_cfg = _pnr_cfg(
        tmp_path,
        CHECKPOINT_STAGES if kind == "checkpoints" else None,
        stages=kind == "stages",
    )
    pnr_cfg.dont_touch = tuple(dont_touch)
    pnr_cfg.hooks = {"after-pdn": (str(hook),)}
    pnr_cfg.resolve_synth_cfg = MagicMock(
        return_value=MagicMock(get_top=lambda: "demo_top", get_name=lambda: "s")
    )
    backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), MagicMock())
    backend._ps_per_unit = 1.0
    if kind != "one session":
        backend._ckpt_run_dir = str(tmp_path / "ck" / "run")
        os.makedirs(backend._ckpt_run_dir, exist_ok=True)
    if template is not None:
        backend._load_template = lambda: template
    platform = _platform(_pdk(tmp_path, pdn_config=str(pdn)))
    script = Path(backend._write_script(platform, pnr_cfg.get_floorplan()))
    written = {"pnr.tcl": script.read_text()}
    if backend._ckpt_run_dir:
        for path in sorted(Path(backend._ckpt_run_dir).glob("*.tcl")):
            written[path.name] = path.read_text()
    return written


def _load(tmp_path, value):
    return PnrConfigFile(
        name="r",
        desc="r",
        synth="s",
        synth_path="synth.yaml",
        platform="p",
        floorplan=PnrFloorplanFile(utilization=0.5),
        dont_touch=value,
    ).initialise(str(tmp_path))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ()),
        ("u_blk.u_clk_buf", ("u_blk.u_clk_buf",)),
        (
            ["u_a", "g_fwd[*].u_buf", r"g\[0\].u"],
            ("u_a", "g_fwd[*].u_buf", r"g\[0\].u"),
        ),
    ],
    ids=["unset", "one name", "a list"],
)
def test_dont_touch_takes_a_name_or_a_list(tmp_path, value, expected):
    assert _load(tmp_path, value).get_dont_touch() == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "dont-touch must be an instance name or glob, or a list of them, got []"),
        (3, "dont-touch must be an instance name or glob, or a list of them, got 3"),
        ({"a": 1}, "dont-touch must be an instance name or glob"),
        (["u_a", ""], "dont-touch[1] must be a non-empty instance name or glob"),
        (["u_a", 7], "dont-touch[1] must be a non-empty instance name or glob"),
        (["u a"], "dont-touch[0] must be a non-empty instance name or glob without"),
        (["u_{a}"], "dont-touch[0] must be"),
        (["u_a\\"], "dont-touch[0] must be"),
        (["u_a", "u_a"], "dont-touch repeats an entry"),
    ],
    ids=[
        "empty list",
        "number",
        "mapping",
        "empty name",
        "number entry",
        "whitespace",
        "brace",
        "trailing backslash",
        "repeat",
    ],
)
def test_a_bad_dont_touch_names_the_run_and_entry(tmp_path, value, message):
    with pytest.raises(FatalRtlBuddyError) as err:
        _load(tmp_path, value)
    assert str(err.value).startswith("pnr run 'r': ")
    assert message in str(err.value)


@pytest.mark.parametrize("kind", ["one session", "checkpoints", "stages"])
def test_without_the_key_every_script_renders_as_the_flow_without_it(tmp_path, kind):
    template = files("rtl_buddy.pnr").joinpath("flow.tcl.template").read_text()
    for placeholder in _PLACEHOLDERS:
        assert template.count(placeholder) == 1
        template = template.replace(placeholder, "")

    now = _scripts(tmp_path, kind=kind)
    before = _scripts(tmp_path, kind=kind, template=template)

    assert now == before
    assert all("dont_touch" not in text for text in now.values())


@pytest.mark.parametrize("kind", ["one session", "stages"])
def test_the_instances_are_dont_touch_from_the_floorplan_and_kept_powered(
    tmp_path, kind
):
    scripts = _scripts(
        tmp_path, dont_touch=("u_blk.u_clk_buf", "g_fwd[*].u_buf"), kind=kind
    )
    text = scripts["pnr.tcl" if kind == "one session" else "01_floorplan.tcl"]
    order = [
        'puts ">>> Dont-touch instances (dont-touch)"\n',
        "proc rb::insts::match",
        "proc rb::dont_touch::restore",
        _APPLY + 'puts ">>> Initializing floorplan"\n',
        # Before the floorplan's setup repair and every later step.
        'puts ">>> Floorplan setup repair"',
        "rb::dont_touch::lift\n",
        'puts ">>> Cut rows around macros"',
        "pdngen\nrb::dont_touch::restore\n",
        ">>> Hook after-pdn: ",
    ]
    positions = [text.index(piece) for piece in order]
    assert positions == sorted(positions)
    assert text.count("rb::dont_touch::apply [") == 1
    # The mark is kept in the database: only the floorplan stage sets it.
    if kind == "stages":
        sessions = {name: s for name, s in scripts.items() if name != "pnr.tcl"}
        assert [n for n, s in sessions.items() if "rb::dont_touch" in s] == [
            "01_floorplan.tcl"
        ]


def test_the_key_enters_the_config_digest_only_when_set(tmp_path):
    cfg = _pnr_cfg(tmp_path, None)
    cfg.resolve_synth_cfg = MagicMock()
    platform = _platform(_pdk(tmp_path))
    default = pnr_abstract.abstract_config(cfg, platform)
    assert "dont_touch" not in default

    cfg.dont_touch = ("u_blk.u_clk_buf",)
    marked = pnr_abstract.abstract_config(cfg, platform)
    assert marked["dont_touch"] == ["u_blk.u_clk_buf"]
    assert pnr_abstract.config_digest(marked) != pnr_abstract.config_digest(default)


#: A block of instances named as OpenROAD stores a Yosys netlist's names, each with its supply pins' nets (`-` for none) and a dont-touch flag; `rules` is the number of global connection rules.
_BLOCK = r"""
namespace eval rb {}
set rules 1
set calls {}
proc rb::global_connect {} { lappend ::calls global_connect }
set insts {
    {g_fwd\[0\].u_buf} {VDD VDD VSS VSS}
    {g_fwd\[1\].u_buf} {VDD VDD VSS VSS}
    u_blk.u_clk_buf {VDD VDD VSS -}
    r0 {VDD VDD VSS VSS}
}
set handles {}
set dont {}
foreach {name supplies} $insts {
    set h rb_inst[dict size $handles]
    interp alias {} $h {} inst $name
    dict set handles $name $h
    dict set dont $name 0
}
proc inst {name cmd args} {
    switch -- $cmd {
        getName { return $name }
        setDoNotTouch { dict set ::dont $name [lindex $args 0] }
        getITerms {
            set out {}
            foreach {pin net} [dict get $::insts $name] {
                set h iterm_[string map {. _ \\ _ [ _ ] _} $name]_$pin
                interp alias {} $h {} iterm $pin $net
                lappend out $h
            }
            return $out
        }
    }
}
proc iterm {pin net cmd args} {
    switch -- $cmd {
        getMTerm { interp alias {} mterm_$pin {} mterm $pin; return mterm_$pin }
        getNet { return [expr {$net eq "-" ? "NULL" : "net_$net"}] }
    }
}
proc mterm {pin cmd args} {
    switch -- $cmd {
        getName { return $pin }
        getSigType { return [expr {$pin eq "VDD" ? "POWER" : "GROUND"}] }
    }
}
proc block {cmd args} {
    switch -- $cmd {
        findInst {
            set name [lindex $args 0]
            if {[dict exists $::handles $name]} { return [dict get $::handles $name] }
            return NULL
        }
        getInsts { return [dict values $::handles] }
        getGlobalConnects { return [lrepeat $::rules rule] }
    }
}
namespace eval ord {}
proc ord::get_db_block {} { return block }
proc puts {args} {}
"""


def _tcl(script: str) -> str:
    source = "".join(
        files("rtl_buddy.pnr").joinpath(name).read_text()
        for name in ("insts.tcl", "dont_touch.tcl")
    )
    return _tcl_eval(_BLOCK + source + "\n" + script)


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("u_blk.u_clk_buf", "u_blk.u_clk_buf"),
        ("g_fwd[0].u_buf", r"{g_fwd\[0\].u_buf}"),
        (r"g_fwd\[0\].u_buf", r"{g_fwd\[0\].u_buf}"),
        ("g_fwd[*].u_buf", r"{g_fwd\[0\].u_buf} {g_fwd\[1\].u_buf}"),
        ("*u_buf", r"{g_fwd\[0\].u_buf} {g_fwd\[1\].u_buf}"),
        ("r?", "r0"),
        ("g_fwd[01].u_buf", ""),
    ],
    ids=[
        "plain name",
        "netlist name",
        "escaped name",
        "literal brackets, star",
        "star",
        "question mark",
        "no character class",
    ],
)
def test_an_instance_is_named_as_floorplan_macros_names_it(pattern, expected):
    result = _tcl(
        f"set result [lmap i [rb::insts::match block {{{pattern}}}] {{$i getName}}]"
    )
    assert result == expected


def test_apply_marks_every_match_and_names_a_pattern_that_matches_none():
    assert (
        _tcl(
            "rb::dont_touch::apply [list [list {dont-touch[0]} {g_fwd[*].u_buf}]]\n"
            "set result $dont"
        )
        == r"{g_fwd\[0\].u_buf} 1 {g_fwd\[1\].u_buf} 1 u_blk.u_clk_buf 0 r0 0"
    )
    assert _tcl(
        "catch {rb::dont_touch::apply [list [list {dont-touch[0]} r0] "
        "[list {dont-touch[1]} u_nope]]} result\nset result"
    ) == ("dont-touch[1]: no instance matches 'u_nope'")


def test_restore_connects_the_supplies_before_marking_and_names_an_unpowered_pin():
    lifted = _tcl(
        "rb::dont_touch::apply [list [list {dont-touch[0]} r0]]\n"
        "rb::dont_touch::lift\n"
        "set lifted [dict get $dont r0]\n"
        "rb::dont_touch::restore\n"
        "set result [list $lifted [dict get $dont r0] $calls]"
    )
    assert lifted == "0 1 global_connect"

    error = _tcl(
        "rb::dont_touch::apply [list [list {dont-touch[0]} u_blk.u_clk_buf]]\n"
        "rb::dont_touch::lift\n"
        "catch rb::dont_touch::restore result\n"
        "set result [list $result [dict get $dont u_blk.u_clk_buf]]"
    )
    assert error.startswith(
        "{dont-touch: u_blk.u_clk_buf supply pin VSS is on no net after the power "
        "grid's global connections;"
    )
    assert error.endswith("} 0")

    # Without global connection rules nothing is connected, so nothing is checked.
    assert (
        _tcl(
            "set rules 0\n"
            "rb::dont_touch::apply [list [list {dont-touch[0]} u_blk.u_clk_buf]]\n"
            "rb::dont_touch::lift\nrb::dont_touch::restore\n"
            "set result [dict get $dont u_blk.u_clk_buf]"
        )
        == "1"
    )
