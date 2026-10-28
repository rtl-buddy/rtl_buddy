"""Tests for the run-level Tcl `hooks:` of `rb pnr`.

Pinned behaviour:

- a point takes one path or a list; unknown points and malformed values fail at load;
- each point renders at its flow position, and a run without hooks renders the flow
  byte for byte as before;
- a hook file is announced, sourced at global scope through `rb::hook::run`, and
  brackets itself in the progress file when stage checkpoints are armed;
- a configured file that is missing fails the run at `setup`, before OpenROAD starts;
- every hook file is a checkpoint-manifest and abstract input, and an abstract's config
  record carries the hooks only when set.

The generated `pnr.tcl` runs under a plain Tcl interpreter with every OpenROAD command
stubbed (see `test_pnr_checkpoints`).
"""

import json
from importlib.resources import files
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest
from test_pnr import (
    _capture_pnr_events,
    _make_pdk_cfg,
    _make_pnr_cfg,
    _one_event,
    _render_flow,
    _touch,
)
from test_pnr import _platform as _flow_platform
from test_pnr_blocks import _block_platform, _block_suite, _ref, _sha, _write
from test_pnr_checkpoints import (
    _HARNESS,
    _REAL_RUN,
    _backend,
    _events,
    _runs,
    _tcl_command,
)

from rtl_buddy.config.pdk import PlacementFile
from rtl_buddy.config.pnr import (
    HOOK_POINTS,
    PnrConfigFile,
    PnrFloorplan,
    PnrFloorplanFile,
    PnrMacro,
    PnrSuiteConfig,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.runner.pnr_results import PnrFailResults, PnrPassResults
from rtl_buddy.tools import pnr_abstract
from rtl_buddy.tools.pnr_abstract import resolve_block
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr, _run_hook_lines


def _cfg_file(**kw):
    return PnrConfigFile(
        name="r", desc="r", synth="s", synth_path="synth.yaml", platform="p", **kw
    )


def _hook_files(tmp_path, points=HOOK_POINTS):
    """One hook file per point, each printing `MARK <point>`, a `stage-end` one also its stage and the run's mode; returns point -> path."""
    paths = {}
    for point in points:
        path = tmp_path / "hooks" / f"{point}.tcl"
        mark = (
            "MARK stage-end $rb_stage $rb_mode"
            if point == "stage-end"
            else f"MARK {point}"
        )
        _write(path, f'puts "{mark}"\n')
        paths[point] = str(path)
    return paths


def _q(path):
    return f'"{path}"'


def _announce(point, path):
    return f'\nputs ">>> Hook {point}: {path}"\nrb::hook::run {point} "{path}"\n'


def _stage_end_announce(stage, path):
    return f'\nputs ">>> Hook stage-end ({stage}): {path}"\nrb::hook::stage_end {stage} "{path}"\n'


#: The stages of a one-session run without post-route repair, each ended by the `stage-end` hooks.
_ONE_SESSION_STAGES = (
    "floorplan",
    "place",
    "cts",
    "global_route",
    "detailed_route",
    "finish",
)


def _announced(point, stage=None):
    """The log line prefix that announces a hook file of `point`; a `stage-end` one names `stage`."""
    return (
        f">>> Hook stage-end ({stage}):"
        if point == "stage-end"
        else f">>> Hook {point}:"
    )


# Configuration


def test_hooks_default_to_none(tmp_path):
    assert _cfg_file().initialise(str(tmp_path)).get_hooks() == {}


def test_a_point_takes_one_path_or_a_list_resolved_from_pnr_yaml(tmp_path):
    cfg = _cfg_file(
        hooks={
            "after-cts": ["a.tcl", "sub/b.tcl"],
            "session": "wire.tcl",
            "before-reports": [],
        }
    ).initialise(str(tmp_path))

    # Flow order, whatever order the YAML writes; an empty list is unset.
    assert cfg.get_hooks() == {
        "session": (str(tmp_path / "wire.tcl"),),
        "after-cts": (str(tmp_path / "a.tcl"), str(tmp_path / "sub/b.tcl")),
    }


def test_hooks_load_from_yaml_relative_to_the_file(tmp_path):
    (tmp_path / "pnr.yaml").write_text(
        dedent("""\
        rtl-buddy-filetype: pnr_config
        runs:
          - name: r
            desc: d
            synth: s
            synth-path: synth.yaml
            platform: p
            hooks:
              macro-placement: macros.tcl
              after-pdn: [pdn_a.tcl, pdn_b.tcl]
              before-detailed-route: []
        """)
    )
    cfg = PnrSuiteConfig(str(tmp_path / "pnr.yaml")).get_runs("r")[0]
    assert cfg.get_hooks() == {
        "macro-placement": (str(tmp_path / "macros.tcl"),),
        "after-pdn": (str(tmp_path / "pdn_a.tcl"), str(tmp_path / "pdn_b.tcl")),
    }


@pytest.mark.parametrize("hooks", [None, {}, {"after-cts": []}, {"after-cts": None}])
def test_unset_and_empty_hooks_are_no_hooks(tmp_path, hooks):
    assert _cfg_file(hooks=hooks).initialise(str(tmp_path)).get_hooks() == {}


def test_an_unknown_point_lists_the_valid_ones(tmp_path):
    with pytest.raises(FatalRtlBuddyError) as err:
        _cfg_file(hooks={"after-cts": "a.tcl", "after-ctss": "b.tcl"}).initialise(
            str(tmp_path)
        )
    message = str(err.value)
    assert message.startswith("pnr run 'r': hooks has unknown point(s) 'after-ctss'")
    assert all(point in message for point in HOOK_POINTS)


@pytest.mark.parametrize(
    "value",
    [3, True, "", [""], ["a.tcl", ""], ["a.tcl", 3], [["a.tcl"]], {"a": "b"}],
)
def test_a_point_needs_non_empty_paths(tmp_path, value):
    with pytest.raises(FatalRtlBuddyError) as err:
        _cfg_file(hooks={"after-cts": value}).initialise(str(tmp_path))
    assert str(err.value).startswith("pnr run 'r': hooks.after-cts must be")


@pytest.mark.parametrize("hooks", ["after-cts.tcl", ["a.tcl"], 5, True])
def test_hooks_must_be_a_mapping(tmp_path, hooks):
    with pytest.raises(FatalRtlBuddyError, match="pnr run 'r': hooks must map"):
        _cfg_file(hooks=hooks).initialise(str(tmp_path))


# Flow positions


def _full_platform(tmp_path, **placement):
    """A platform whose flow renders every block a hook point sits next to."""
    pdk = _make_pdk_cfg(
        tmp_path,
        pdn_config="pdk/pdn.tcl",
        layer_rc_tcl="pdk/setRC.tcl",
        tapcell_tcl="pdk/tapcell.tcl",
        rcx_rules="pdk/rcx.rules",
        dont_use_cells=["AND2_X1"],
        **({"placement": PlacementFile(**placement)} if placement else {}),
    )
    return _flow_platform(
        pdk, cts_buffer="BUF_X4", max_fanout=8, global_route_hold_repair=True
    )


# Flow-order anchors; each hook marker sits between the anchors around it.
_FLOW_ORDER = [
    "read_sdc $SDC_FILE",
    "set_max_fanout 8",
    ">>> Layer RC (layer-rc-tcl)",
    "set_dont_use [list AND2_X1]",
    ">>> Hook session:",
    ">>> Initializing floorplan",
    "insert_tiecells $TIEHI_CELL_PORT",
    ">>> Hook after-floorplan:",
    ">>> Macro placement",
    "if {[llength $macros] > 0} {",
    ">>> Hook macro-placement:",
    "set cell_halo [list $MACRO_CELL_HALO $MACRO_CELL_HALO]",
    ">>> Tap and endcap cells (tapcell-tcl)",
    "pdngen",
    ">>> Hook after-pdn:",
    ">>> IO pin placement",
    "place_pins -hor_layers",
    ">>> Hook after-pins:",
    ">>> Port buffers",
    ">>> Hook stage-end (floorplan):",
    ">>> Global placement",
    "\nglobal_placement -density",
    ">>> Hook after-global-placement:",
    ">>> Repair tie fanout",
    ">>> Resize",
    "\nrepair_design\n",
    ">>> Hook after-repair-design:",
    ">>> Detail placement (legalize)",
    ">>> Hook stage-end (place):",
    ">>> Clock tree synthesis",
    "set_propagated_clock [all_clocks]",
    ">>> Hook before-cts:",
    "clock_tree_synthesis \\",
    "-buf_list $CTS_BUF",
    ">>> Hook before-repair-clock-nets:",
    ">>> Hook after-cts:",
    ">>> Detail placement (post-CTS)",
    "repair_timing -hold",
    ">>> Detail placement (legalize repair cells)",
    "check_placement -verbose",
    ">>> Hook after-cts-repair:",
    ">>> Hook stage-end (cts):",
    "\nglobal_route -congestion_iterations",
    ">>> Hook after-global-route:",
    ">>> Post-global-route hold repair",
    ">>> Hook after-global-route-repair:",
    ">>> Hook stage-end (global_route):",
    ">>> Hook before-detailed-route:",
    ">>> Detail route",
    "detailed_route \\",
    ">>> Hook stage-end (detailed_route):",
    ">>> Fill insertion",
    "filler_placement $FILL_CELLS",
    ">>> Parasitic extraction (OpenRCX)",
    "write_spef $OUT_DIR/${DESIGN}.routed.spef",
    ">>> Hook after-extraction:",
    ">>> Hook before-reports:",
    ">>> Final reports",
    "read_spef $OUT_DIR/${DESIGN}.routed.spef",
    "electrical.rpt",
    ">>> Hook after-reports:",
    ">>> Hook stage-end (finish):",
    ">>> Write outputs",
]


def test_every_point_renders_at_its_flow_position(tmp_path):
    hooks = {point: (path,) for point, path in _hook_files(tmp_path).items()}
    text = _render_flow(
        tmp_path, _full_platform(tmp_path), hooks=hooks, buffer_ports=True
    )

    position = 0
    for anchor in _FLOW_ORDER:
        position = text.index(anchor, position)
    # `HOOK_POINTS` is in flow order, with `stage-end`, which ends every stage, last.
    points = [
        a.removeprefix(">>> Hook ").removesuffix(":")
        for a in _FLOW_ORDER
        if a.startswith(">>> Hook ") and "stage-end" not in a
    ]
    assert [*points, "stage-end"] == list(HOOK_POINTS)
    for point in HOOK_POINTS:
        if point == "stage-end":
            continue
        assert text.count(f'puts ">>> Hook {point}: ') == 1
    for stage in _ONE_SESSION_STAGES:
        assert text.count(f'puts ">>> Hook stage-end ({stage}): ') == 1
    assert text.count('puts ">>> Hook stage-end (') == len(_ONE_SESSION_STAGES)


def test_a_run_without_hooks_renders_the_flow_unchanged(tmp_path):
    """Setting every point adds only its blocks; the macro-placement block replaces the packer.

    A hook before global placement also arms `placement.rotated-macro-footprint`, which is off here.
    """
    platform = _full_platform(tmp_path, rotated_macro_footprint="none")
    unset = _render_flow(tmp_path, platform, suite_dir=tmp_path / "a")
    paths = _hook_files(tmp_path)
    hooked = _render_flow(
        tmp_path,
        platform,
        suite_dir=tmp_path / "a",
        hooks={point: (path,) for point, path in paths.items()},
    )

    assert "rb::hook" not in unset and "Hook" not in unset
    stripped = hooked
    pnr = files("rtl_buddy.pnr")
    procs = (
        "\n"
        + pnr.joinpath("hooks.tcl").read_text()
        + "\n"
        + pnr.joinpath("stage_end_hook.tcl").read_text()
        + "set rb_mode closure\n"
    )
    assert hooked.count(procs) == 1
    stripped = stripped.replace(procs, "")
    for point, path in paths.items():
        if point == "macro-placement":
            continue
        if point == "stage-end":
            blocks = [_stage_end_announce(s, path) for s in _ONE_SESSION_STAGES]
        elif point == "after-extraction":
            spef = "$OUT_DIR/${DESIGN}.routed.spef"
            blocks = [f"\nset rb_spef {spef}" + _announce(point, path)]
        else:
            blocks = [_announce(point, path)]
        for block in blocks:
            assert hooked.count(block) == 1
            stripped = stripped.replace(block, "")
    macro_lines = (
        "  set rb_movable_macros $macros\n"
        f'  puts ">>> Hook macro-placement: {paths["macro-placement"]}"\n'
        f'  rb::hook::run macro-placement "{paths["macro-placement"]}"\n'
    )
    assert hooked.count(macro_lines) == 1
    packer = unset[unset.index("  set core [$block getCoreArea]") :]
    packer = packer[: packer.index("  # A hard placement blockage")]
    assert stripped.replace(macro_lines, packer) == unset


def test_an_empty_hooks_mapping_renders_the_flow_unchanged(tmp_path):
    platform = _full_platform(tmp_path)
    unset = _render_flow(tmp_path, platform, suite_dir=tmp_path / "a")
    cfg = _cfg_file(hooks={"after-cts": [], "session": []}).initialise(str(tmp_path))
    emptied = _render_flow(
        tmp_path, platform, suite_dir=tmp_path / "a", hooks=cfg.get_hooks()
    )
    assert emptied == unset


def test_the_proc_is_defined_only_with_a_hook_and_before_the_first_one(tmp_path):
    platform = _full_platform(tmp_path)
    paths = _hook_files(tmp_path, ("session", "before-reports"))
    text = _render_flow(
        tmp_path,
        platform,
        hooks={point: (path,) for point, path in paths.items()},
    )
    assert text.count("proc rb::hook::run") == 1
    assert text.index("proc rb::hook::run") < text.index("rb::hook::run session")
    assert "proc rb::hook::run" not in _render_flow(tmp_path, platform)


def test_files_of_one_point_run_in_list_order(tmp_path):
    first, second = (str(tmp_path / n) for n in ("first.tcl", "second.tcl"))
    text = _render_flow(
        tmp_path,
        _full_platform(tmp_path),
        hooks={"after-cts": (second, first)},
    )
    assert text.index(f"rb::hook::run after-cts {_q(second)}") < text.index(
        f"rb::hook::run after-cts {_q(first)}"
    )


def test_the_macro_placement_hook_replaces_the_packer_after_the_directives(tmp_path):
    hook = str(tmp_path / "macros.tcl")
    floorplan = PnrFloorplan(
        utilization=0.55,
        aspect=1.0,
        core_margin=2.0,
        macros=[PnrMacro("u_sram", location=(30.0, 20.0))],
    )
    text = _render_flow(
        tmp_path,
        _full_platform(tmp_path),
        floorplan=floorplan,
        hooks={"macro-placement": (hook,)},
    )

    apply_at = text.index("rb::macros::apply $block $macros")
    hook_at = text.index("rb::hook::run macro-placement")
    halo_at = text.index("set cell_halo [list $MACRO_CELL_HALO")
    assert apply_at < hook_at < halo_at
    assert "rb::macro_pack::solve \\" not in text
    # The macro branch and its keep-out halos are still there around the hook.
    assert text.index("if {[llength $macros] > 0} {") < apply_at


@pytest.mark.parametrize(
    "floorplan, key",
    [
        ({"macro_placement": "rtl-mp"}, "floorplan.macro-placement"),
        ({"macro_anchor": "upper-right"}, "floorplan.macro-anchor"),
    ],
)
def test_macro_placement_keys_are_rejected_with_a_macro_placement_hook(
    tmp_path, floorplan, key
):
    """They would have no effect, as `macro-anchor` has none with `rtl-mp`."""
    with pytest.raises(FatalRtlBuddyError) as err:
        _cfg_file(
            floorplan=PnrFloorplanFile(utilization=0.5, **floorplan),
            hooks={"macro-placement": "macros.tcl"},
        ).initialise(str(tmp_path))
    assert str(err.value).startswith(
        f"pnr run 'r': '{key}' steers rb's macro placement and has no effect "
        "with a 'macro-placement' hook"
    )


def test_the_macro_placement_hook_gets_every_macro_as_movable_without_directives(
    tmp_path,
):
    text = _render_flow(
        tmp_path,
        _full_platform(tmp_path),
        hooks={"macro-placement": (str(tmp_path / "macros.tcl"),)},
    )
    branch = text[text.index("if {[llength $macros] > 0} {") :]
    assert branch.index("  set rb_movable_macros $macros\n") < branch.index(
        "rb::hook::run macro-placement"
    )


def test_the_hook_runs_between_global_placement_and_tie_fanout_repair(tmp_path):
    """The seam a later step between `global_placement` and the hook slots into."""
    text = _render_flow(
        tmp_path,
        _full_platform(tmp_path),
        hooks={"after-global-placement": (str(tmp_path / "gp.tcl"),)},
    )
    gp = text.index("\nglobal_placement -density")
    hook = text.index("rb::hook::run after-global-placement")
    tie = text.index(">>> Repair tie fanout")
    assert gp < hook < tie
    assert text[gp:hook].count("\n") <= 4


def test_pin_constraints_stay_where_they_are(tmp_path):
    pin = tmp_path / "pins.tcl"
    pin.write_text("# pins\n")
    text = _render_flow(
        tmp_path,
        _full_platform(tmp_path),
        pin_constraints=str(pin),
        hooks={
            "after-pdn": (str(tmp_path / "pdn.tcl"),),
            "after-pins": (str(tmp_path / "pins_after.tcl"),),
        },
    )
    source = text.index(f'source "{pin}"')
    assert text.index("rb::hook::run after-pdn") < source
    assert source < text.index("place_pins -hor_layers")
    assert text.index("place_pins -hor_layers") < text.index("rb::hook::run after-pins")


# Running the generated script under Tcl


def _tcl_run(tmp_path, script, name="snippet"):
    """Run a Tcl snippet in a plain interpreter; returns the completed process."""
    harness = tmp_path / f"{name}.harness.tcl"
    harness.write_text("source [lindex $argv 0]\n")
    snippet = tmp_path / f"{name}.tcl"
    snippet.write_text(script)
    return _REAL_RUN(
        _tcl_command(harness, str(snippet)),
        capture_output=True,
        text=True,
        check=False,
    )


_HOOKS_TCL = files("rtl_buddy.pnr").joinpath("hooks.tcl").read_text()


def test_a_hook_is_sourced_at_global_scope(tmp_path):
    hook = _write(
        tmp_path / "scope.tcl",
        'puts "SEES $DESIGN level [info level]"\nset rb_from_hook 7\nreturn\nputs "SKIPPED"\n',
    )
    done = _tcl_run(
        tmp_path,
        _HOOKS_TCL
        + f"set DESIGN demo_top\nproc in_a_proc {{}} {{ rb::hook::run after-cts {_q(hook)} }}\n"
        + 'in_a_proc\nputs "AFTER $rb_from_hook"\n',
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["SEES demo_top level 0", "AFTER 7"]


def test_a_path_with_tcl_metacharacters_is_inert(tmp_path):
    hook = _write(tmp_path / 'a $[x] "q" ;b.tcl', 'puts "RAN"\n')
    lines = _run_hook_lines("after-cts", (str(hook),))
    done = _tcl_run(tmp_path, _HOOKS_TCL + lines)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [f">>> Hook after-cts: {hook}", "RAN"]


def test_a_path_with_braces_is_inert_inside_the_macro_branch(tmp_path):
    hook = _write(tmp_path / "a }x{ y.tcl", 'puts "RAN"\n')
    lines = _run_hook_lines("macro-placement", (str(hook),), "  ")
    done = _tcl_run(tmp_path, _HOOKS_TCL + f"if {{1}} {{\n{lines}}}\n")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [f">>> Hook macro-placement: {hook}", "RAN"]


@pytest.mark.parametrize(
    "command, message",
    [
        ("break", "invoked break outside of a loop"),
        ("continue", "invoked continue outside of a loop"),
        ("return -level 3", "return reaches past the hook file"),
        ("return -code 7", "ended with completion code 7"),
    ],
)
def test_a_completion_that_would_end_the_flow_is_a_named_error(
    tmp_path, command, message
):
    hook = _write(tmp_path / "loop.tcl", f"{command}\n")
    done = _tcl_run(
        tmp_path,
        _HOOKS_TCL + f"set code [catch {{rb::hook::run after-cts {_q(hook)}}} msg]\n"
        'puts "CODE $code"\nputs "MSG $msg"\n',
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "CODE 1",
        f"MSG Hook after-cts ({hook}): {message}",
    ]


def test_exit_in_a_hook_is_an_error_and_exit_is_restored(tmp_path):
    hook = _write(tmp_path / "quit.tcl", "exit 0\n")
    nested = _write(tmp_path / "nested.tcl", f"rb::hook::run after-cts {_q(hook)}\n")
    done = _tcl_run(
        tmp_path,
        _HOOKS_TCL + f"set code [catch {{rb::hook::run after-cts {_q(hook)}}} msg]\n"
        'puts "CODE $code"\nputs "MSG $msg"\n'
        + f"set code [catch {{rb::hook::run session {_q(nested)}}} msg]\n"
        'puts "NESTED $code"\n'
        'puts "RESTORED [llength [info commands ::rb::hook::exit]]"\n'
        "exit 3\n",
    )
    assert done.returncode == 3, done.stderr
    assert done.stdout.splitlines() == [
        "CODE 1",
        f"MSG Hook after-cts ({hook}): exit 0: a hook must return, not exit, "
        "so the flow can go on",
        "NESTED 1",
        "RESTORED 0",
    ]


def test_custom_error_info_still_leads_with_the_prefix(tmp_path):
    hook = _write(tmp_path / "custom.tcl", 'error boom "detail line"\n')
    done = _tcl_run(
        tmp_path,
        _HOOKS_TCL + f"catch {{rb::hook::run after-cts {_q(hook)}}} msg opts\n"
        'puts "INFO [dict get $opts -errorinfo]"\n',
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith(f"INFO Hook after-cts ({hook}): boom\ndetail line")


def test_a_run_killed_in_a_hook_command_names_the_hook():
    from rtl_buddy.tools.pnr_checkpoints import last_step

    events = [
        {"event": "step_begin", "step": "place_pins"},
        {"event": "step_end", "step": "place_pins", "status": "ok"},
        {"event": "hook_begin", "point": "after-pins"},
        {"event": "step_begin", "step": "global_placement"},
    ]
    assert last_step(events) == {"step": "hook after-pins", "status": "running"}


def test_an_empty_error_still_leads_errorinfo_with_the_prefix(tmp_path):
    hook = _write(tmp_path / "empty.tcl", 'error ""\n')
    done = _tcl_run(
        tmp_path,
        _HOOKS_TCL + f"catch {{rb::hook::run after-cts {_q(hook)}}} msg opts\n"
        'puts "INFO [dict get $opts -errorinfo]"\n',
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith(f"INFO Hook after-cts ({hook}): \n")


def test_an_error_names_the_point_and_path_and_keeps_errorinfo(tmp_path):
    hook = _write(tmp_path / "bad.tcl", 'set x 1\nerror "boom"\n')
    done = _tcl_run(
        tmp_path,
        _HOOKS_TCL
        + f"set code [catch {{rb::hook::run after-cts {_q(hook)}}} msg opts]\n"
        'puts "CODE $code"\nputs "MSG $msg"\n'
        'puts "INFO [dict get $opts -errorinfo]"\n',
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "CODE 1" in out
    assert f"MSG Hook after-cts ({hook}): boom" in out
    info = out[out.index("INFO ") + len("INFO ") :]
    # OpenROAD prints errorInfo, so its first line carries the prefix and the trace follows.
    assert info.startswith(f"Hook after-cts ({hook}): boom\n")
    assert f'(file "{hook}" line 2)' in info


def test_a_multi_line_error_keeps_every_line_under_the_prefix(tmp_path):
    hook = _write(tmp_path / "bad.tcl", 'error "first line\nsecond line"\n')
    done = _tcl_run(
        tmp_path,
        _HOOKS_TCL + f"catch {{rb::hook::run after-pins {_q(hook)}}} msg opts\n"
        'puts "INFO [dict get $opts -errorinfo]"\n',
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith(
        f"INFO Hook after-pins ({hook}): first line\nsecond line\n    while executing"
    )


def test_hook_events_need_no_checkpoints(tmp_path):
    hook = _write(tmp_path / "plain.tcl", 'puts "PLAIN"\n')
    done = _tcl_run(tmp_path, _HOOKS_TCL + f"rb::hook::run session {_q(hook)}\n")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "PLAIN"


# Full flow under the stub OpenROAD

_MACRO_STUBS = """
proc ord::get_db_block {} { return rb_blk }
proc rb_blk {cmd args} {
    switch $cmd {
        getInsts { return {rb_macro} }
        default { return "" }
    }
}
proc rb_macro {cmd args} {
    switch $cmd {
        getMaster { return rb_master }
        getName { return rb_macro }
        getBBox { return rb_bbox }
        default { return "" }
    }
}
proc rb_master {cmd args} { return [expr {$cmd eq "isBlock"}] }
proc rb_bbox {cmd args} { return 0 }
"""


def _hooked_backend(
    tmp_path, monkeypatch, hooks, *, checkpoints=True, macros=False, **options
):
    """A stub-OpenROAD backend whose run has `hooks`; `macros` puts one macro in the netlist. `options` are `_backend` keywords."""
    from rtl_buddy.config.pnr import CHECKPOINT_STAGES

    backend, artefacts = _backend(
        tmp_path,
        monkeypatch,
        checkpoints=CHECKPOINT_STAGES if checkpoints else None,
        **options,
    )
    backend.pnr_cfg.hooks = hooks
    if macros:
        (tmp_path / "harness.tcl").write_text(
            _HARNESS.replace(
                "source [lindex $argv 0]", _MACRO_STUBS + "source [lindex $argv 0]"
            )
        )
    return backend, artefacts


def _log_lines(artefacts):
    return (artefacts / "pnr.log").read_text().splitlines()


#: Where each hook file runs in a one-session run: a point, or `stage-end` and the stage it ends.
_RUN_ORDER = [
    ("session", None),
    ("after-floorplan", None),
    ("macro-placement", None),
    ("after-pdn", None),
    ("after-pins", None),
    ("stage-end", "floorplan"),
    ("after-global-placement", None),
    ("after-repair-design", None),
    ("stage-end", "place"),
    ("before-cts", None),
    ("before-repair-clock-nets", None),
    ("after-cts", None),
    ("after-cts-repair", None),
    ("stage-end", "cts"),
    ("after-global-route", None),
    ("after-global-route-repair", None),
    ("stage-end", "global_route"),
    ("before-detailed-route", None),
    ("stage-end", "detailed_route"),
    ("after-extraction", None),
    ("before-reports", None),
    ("after-reports", None),
    ("stage-end", "finish"),
]


def _rcx_rules(tmp_path) -> dict:
    """The PDK fields of a stub run that extracts, which `after-extraction` needs."""
    return {"rcx_rules": str(_write(tmp_path / "pdk" / "rcx.rules", "rules\n"))}


def test_every_hook_runs_in_flow_order_in_the_real_script(tmp_path, monkeypatch):
    paths = _hook_files(tmp_path)
    backend, artefacts = _hooked_backend(
        tmp_path,
        monkeypatch,
        {point: (path,) for point, path in paths.items()},
        macros=True,
        pdk_fields=_rcx_rules(tmp_path),
    )

    res = backend.run()

    assert isinstance(res, PnrPassResults), res.results
    log = _log_lines(artefacts)
    announced = [line for line in log if line.startswith(">>> Hook ")]
    assert announced == [f"{_announced(p, s)} {paths[p]}" for p, s in _RUN_ORDER]
    marks = [line for line in log if line.startswith("MARK ")]
    assert marks == [
        f"MARK stage-end {s} closure" if p == "stage-end" else f"MARK {p}"
        for p, s in _RUN_ORDER
    ]
    # Each marker follows its announcement and nothing of another hook is between them.
    for (point, stage), mark in zip(_RUN_ORDER, marks):
        at = log.index(f"{_announced(point, stage)} {paths[point]}")
        assert log[at + 1] == mark
    # The flow's own stage lines surround them in the documented order.
    stages = [line for line in log if line.startswith(">>> ")]
    order = [
        ">>> Reading SDC",
        ">>> Hook session:",
        ">>> Initializing floorplan",
        ">>> Hook after-floorplan:",
        ">>> Macro placement",
        ">>> Hook macro-placement:",
        ">>> Hook after-pdn:",
        ">>> IO pin placement",
        ">>> Hook after-pins:",
        ">>> Global placement",
        ">>> Hook after-global-placement:",
        ">>> Resize",
        ">>> Hook after-repair-design:",
        ">>> Clock tree synthesis",
        ">>> Hook after-cts:",
        ">>> Detail placement (post-CTS)",
        ">>> Global route",
        ">>> Hook after-global-route:",
        ">>> Hook before-detailed-route:",
        ">>> Detail route",
        ">>> Fill insertion",
        ">>> Hook before-reports:",
        ">>> Final reports",
    ]
    position = 0
    for prefix in order:
        position = next(
            i for i in range(position, len(stages)) if stages[i].startswith(prefix)
        )


def test_the_macro_placement_hook_sees_the_flow_variables(tmp_path, monkeypatch):
    hook = _write(
        tmp_path / "macros.tcl",
        'puts "SEES [llength $macros] [llength $rb_movable_macros] $block $DESIGN '
        '[file tail $OUT_DIR]"\n',
    )
    backend, artefacts = _hooked_backend(
        tmp_path, monkeypatch, {"macro-placement": (str(hook),)}, macros=True
    )

    assert isinstance(backend.run(), PnrPassResults)
    assert "SEES 1 1 rb_blk demo_top demo_pnr" in _log_lines(artefacts)


def test_the_macro_placement_hook_does_not_run_without_macros(tmp_path, monkeypatch):
    hook = _write(tmp_path / "macros.tcl", 'puts "MARK macro-placement"\n')
    backend, artefacts = _hooked_backend(
        tmp_path, monkeypatch, {"macro-placement": (str(hook),)}
    )

    assert isinstance(backend.run(), PnrPassResults)
    assert "MARK macro-placement" not in _log_lines(artefacts)


def test_variables_a_hook_sets_reach_later_hooks(tmp_path, monkeypatch):
    first = _write(tmp_path / "a.tcl", 'set rb_probe "$DESIGN-[file tail $OUT_DIR]"\n')
    second = _write(tmp_path / "b.tcl", 'puts "PROBE $rb_probe"\n')
    backend, artefacts = _hooked_backend(
        tmp_path,
        monkeypatch,
        {"after-floorplan": (str(first),), "after-pins": (str(second),)},
    )

    assert isinstance(backend.run(), PnrPassResults)
    assert "PROBE demo_top-demo_pnr" in _log_lines(artefacts)


def test_hook_events_bracket_each_file_in_the_progress_file(tmp_path, monkeypatch):
    first = _write(tmp_path / "a.tcl", "set a 1\n")
    second = _write(tmp_path / "b.tcl", "set b 2\n")
    backend, artefacts = _hooked_backend(
        tmp_path,
        monkeypatch,
        {"after-cts": (str(first), str(second)), "session": (str(first),)},
    )

    assert isinstance(backend.run(), PnrPassResults)
    events = _events(_runs(artefacts)[0])
    hooks = [e for e in events if e["event"].startswith("hook_")]
    assert [(e["event"], e["point"], e["path"]) for e in hooks] == [
        ("hook_begin", "session", str(first)),
        ("hook_end", "session", str(first)),
        ("hook_begin", "after-cts", str(first)),
        ("hook_end", "after-cts", str(first)),
        ("hook_begin", "after-cts", str(second)),
        ("hook_end", "after-cts", str(second)),
    ]
    for end in (e for e in hooks if e["event"] == "hook_end"):
        assert end["status"] == "ok"
        assert isinstance(end["elapsed_s"], float) and end["elapsed_s"] >= 0
        assert "error" not in end
    names = [e["event"] for e in events]
    cts_end = next(
        i
        for i, e in enumerate(events)
        if e["event"] == "step_end" and e["step"] == "clock_tree_synthesis"
    )
    hook_at = next(
        i
        for i, e in enumerate(events)
        if e["event"] == "hook_begin" and e["point"] == "after-cts"
    )
    post_cts = next(
        i
        for i, e in enumerate(events)
        if e["event"] == "step_begin"
        and i > cts_end
        and e["step"] == "detailed_placement"
    )
    assert cts_end < hook_at < post_cts
    assert names.index("flow_end") > hook_at


def test_a_failing_hook_stops_the_flow_and_is_recorded(tmp_path, monkeypatch):
    hook = _write(tmp_path / "bad.tcl", 'error "boom \\"quoted\\"\\nsecond line"\n')
    backend, artefacts = _hooked_backend(
        tmp_path, monkeypatch, {"after-cts": (str(hook),)}
    )

    res = backend.run()

    assert isinstance(res, PnrFailResults)
    events = _events(_runs(artefacts)[0])
    (end,) = [e for e in events if e["event"] == "hook_end"]
    assert end["status"] == "error"
    assert end["point"] == "after-cts" and end["path"] == str(hook)
    assert end["error"] == 'boom "quoted"\nsecond line'
    # The flow stopped there: nothing after the hook ran.
    assert not any(
        e["event"] == "step_begin" and e["step"] in {"global_route", "detailed_route"}
        for e in events
    )
    assert "DONE" not in "\n".join(_log_lines(artefacts))


def test_a_failing_hook_is_the_last_step(tmp_path, monkeypatch):
    from rtl_buddy.tools.pnr_checkpoints import last_step

    hook = _write(tmp_path / "bad.tcl", 'error "boom"\n')
    backend, artefacts = _hooked_backend(
        tmp_path, monkeypatch, {"after-cts": (str(hook),)}
    )

    assert isinstance(backend.run(), PnrFailResults)
    assert last_step(_events(_runs(artefacts)[0])) == {
        "step": "hook after-cts",
        "status": "error",
        "error": "boom",
    }


_REPORT_STUBS = r"""
proc report_worst_slack {args} {
    set kind [expr {"-min" in $args ? "min" : "max"}]
    puts "worst slack $kind [expr {[info exists ::rb_final] ? "1.000" : "-9.000"}]"
}
proc report_tns {args} {
    set kind [expr {"-min" in $args ? "min " : ""}]
    puts "tns $kind[expr {[info exists ::rb_final] ? "0.00" : "-50.00"}]"
}
proc report_design_area {args} {
    puts "Design area [expr {[info exists ::rb_final] ? 100 : 7}] um^2 50% utilization."
}
"""


def test_a_report_a_hook_prints_does_not_become_the_result(tmp_path, monkeypatch):
    early = _write(
        tmp_path / "clock_report.tcl",
        "report_worst_slack -max\nreport_worst_slack -min\nreport_tns\n"
        "report_tns -min\nreport_design_area\n",
    )
    final = _write(tmp_path / "final.tcl", "set rb_final 1\n")
    backend, _artefacts = _hooked_backend(
        tmp_path,
        monkeypatch,
        {"after-cts": (str(early),), "before-reports": (str(final),)},
    )
    harness = tmp_path / "harness.tcl"
    harness.write_text(
        harness.read_text().replace(
            "source [lindex $argv 0]", _REPORT_STUBS + "source [lindex $argv 0]"
        )
    )

    res = backend.run()

    assert isinstance(res, PnrPassResults), res.results
    assert res.results["area_um2"] == 100.0
    assert res.results["wns_setup_ps"] > 0 and res.results["wns_hold_ps"] > 0
    assert res.results["tns_ps"] == 0.0 and res.results["tns_hold_ps"] == 0.0


def test_hooks_run_without_checkpoints(tmp_path, monkeypatch):
    hook = _write(tmp_path / "h.tcl", 'puts "MARK after-cts"\n')
    backend, artefacts = _hooked_backend(
        tmp_path, monkeypatch, {"after-cts": (str(hook),)}, checkpoints=False
    )

    assert isinstance(backend.run(), PnrPassResults)
    assert "MARK after-cts" in _log_lines(artefacts)
    assert not (artefacts / "checkpoints").exists()


def test_a_command_a_hook_runs_does_not_move_a_stage_checkpoint(tmp_path, monkeypatch):
    """The `floorplan` checkpoint is written on entering the flow's own `global_placement`, not a hook's."""
    hook = _write(tmp_path / "early.tcl", "global_placement -skip_io\n")
    backend, artefacts = _hooked_backend(
        tmp_path, monkeypatch, {"after-floorplan": (str(hook),)}
    )

    assert isinstance(backend.run(), PnrPassResults)
    events = _events(_runs(artefacts)[0])
    kinds = [
        (e["event"], e.get("stage") or e.get("step") or e.get("point")) for e in events
    ]
    hook_end = kinds.index(("hook_end", "after-floorplan"))
    written = kinds.index(("checkpoint_begin", "floorplan"))
    assert hook_end < written
    # The hook's own call still reports its progress.
    assert ("step_begin", "global_placement") in kinds[:hook_end]


# Setup


def test_a_missing_hook_file_fails_at_setup_before_openroad(tmp_path, monkeypatch):
    from rtl_buddy.tools import pnr_openroad

    present = _write(tmp_path / "present.tcl", "")
    missing = tmp_path / "gone.tcl"
    backend, artefacts = _hooked_backend(
        tmp_path,
        monkeypatch,
        {"before-reports": (str(present),), "after-cts": (str(present), str(missing))},
    )
    launched = []
    monkeypatch.setattr(
        pnr_openroad.subprocess, "run", lambda *a, **kw: launched.append(a)
    )
    events = _capture_pnr_events(monkeypatch)

    res = backend.run()

    assert isinstance(res, PnrFailResults)
    assert res.results["fail_stage"] == "setup"
    assert res.results["desc"] == f"hooks.after-cts[1] not found: {missing}"
    assert not launched
    assert not Path(backend._script_path()).exists()
    # Refused before a checkpoint run directory is allocated.
    assert not (artefacts / "checkpoints").exists()
    level, fields = _one_event(events, "pnr.run_hook_missing")
    assert (fields["point"], fields["index"], fields["path"]) == (
        "after-cts",
        1,
        str(missing),
    )
    from rtl_buddy.logging_utils import _human_message

    assert _human_message("pnr.run_hook_missing", fields) == (
        f'P&R "demo_pnr": hooks.after-cts[1] not found: {missing}'
    )


# Manifest and abstract inputs


def _inputs_backend(tmp_path, hooks):
    cfg = _make_pnr_cfg(tmp_path, hooks=hooks)
    cfg.resolve_synth_cfg = MagicMock(
        return_value=MagicMock(get_top=lambda: "t", get_name=lambda: "s")
    )
    return cfg, OpenRoadPnr("demo/openroad", cfg, str(tmp_path), MagicMock())


def test_hook_files_are_checkpoint_and_abstract_inputs(tmp_path):
    platform = _flow_platform(_make_pdk_cfg(tmp_path), cts_buffer="BUF_X4")
    paths = {
        "session": (str(tmp_path / "wire.tcl"),),
        "after-cts": (str(tmp_path / "a.tcl"), str(tmp_path / "b.tcl")),
    }
    for group in paths.values():
        _touch(*group)
    script = str(tmp_path / "pnr.tcl")
    _touch(script)

    plain_cfg, plain = _inputs_backend(tmp_path, {})
    cfg, backend = _inputs_backend(tmp_path, paths)

    assert not any(
        k.startswith("hook_") for k in plain._checkpoint_inputs(platform, script)
    )
    assert not any(k.startswith("hook_") for k in plain.abstract_inputs(platform))

    manifest = backend._checkpoint_inputs(platform, script)
    assert [r["path"] for r in manifest["hook_session"]] == list(paths["session"])
    assert [r["path"] for r in manifest["hook_after_cts"]] == list(paths["after-cts"])
    assert all(r["sha256"] for r in manifest["hook_after_cts"])
    inputs = backend.abstract_inputs(platform)
    assert inputs["hook_session"] == list(paths["session"])
    assert inputs["hook_after_cts"] == list(paths["after-cts"])


def test_hooks_join_the_abstract_config_only_when_set(tmp_path):
    platform = _flow_platform(_make_pdk_cfg(tmp_path), cts_buffer="BUF_X4")
    plain = pnr_abstract.abstract_config(_make_pnr_cfg(tmp_path), platform)
    assert "hooks" not in plain

    hooked_cfg = _make_pnr_cfg(
        tmp_path,
        hooks={"after-cts": (str(tmp_path / "a.tcl"), str(tmp_path / "b.tcl"))},
    )
    hooked = pnr_abstract.abstract_config(hooked_cfg, platform)
    assert hooked["hooks"]["after-cts"][0].endswith("a.tcl")
    assert {k: v for k, v in hooked.items() if k != "hooks"} == plain
    assert pnr_abstract.config_digest(hooked) != pnr_abstract.config_digest(plain)

    reordered = _make_pnr_cfg(
        tmp_path,
        hooks={"after-cts": (str(tmp_path / "b.tcl"), str(tmp_path / "a.tcl"))},
    )
    assert pnr_abstract.config_digest(
        pnr_abstract.abstract_config(reordered, platform)
    ) != pnr_abstract.config_digest(hooked)


def _stale_changes(tmp_path, suite):
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = _block_platform(tmp_path)
    return pnr_abstract.block_changes(resolve_block(_ref(suite)), root_cfg)


def test_editing_a_hook_makes_a_hardened_block_stale(tmp_path):
    suite = _block_suite(tmp_path)
    assert _stale_changes(tmp_path, suite) == []

    hook = _write(tmp_path / "blk/cts.tcl", "set a 1\n")
    suite.write_text(suite.read_text() + "    hooks:\n      after-cts: [cts.tcl]\n")
    # A hook added to the config changes the digest.
    assert any("config changed (hooks)" in c for c in _stale_changes(tmp_path, suite))

    # Record the hooked block as hardened, then edit the hook file.
    manifest_path = tmp_path / "blk/artefacts/blk_pnr/abstract/abstract.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    from rtl_buddy.config.pnr import PnrSuiteConfig as _Suite

    run_cfg = _Suite(str(suite)).get_runs("blk_pnr")[0]
    config = pnr_abstract.abstract_config(run_cfg, _block_platform(tmp_path))
    manifest["config"] = {**config, "sha256": pnr_abstract.config_digest(config)}
    manifest["inputs"]["hook_after_cts"] = [
        {"path": str(hook), "sha256": _sha("set a 1\n")}
    ]
    manifest_path.write_text(json.dumps(manifest))
    assert _stale_changes(tmp_path, suite) == []

    hook.write_text("set a 2\n")
    changes = _stale_changes(tmp_path, suite)
    assert changes == [f"hook_after_cts {hook} changed"]
