"""Tests for `placement.macro-pin-limits`, the macro pin-limit repair step (rtl_buddy#838).

The pure-Tcl helpers in `rtl_buddy/pnr/macro_pin_limits.tcl` run under a bare Tcl
interpreter; the OpenROAD half is checked by its place in the rendered flow.
"""

import logging
from importlib.resources import files
from textwrap import dedent

import pytest
from serde.yaml import from_yaml

from rtl_buddy.config.pdk import MacroPinLimitsFile, PlacementFile, validate_placement
from rtl_buddy.config.pnr_platform import PnrPlatformConfig, PnrPlatformConfigFile
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.logging_utils import _human_message
from rtl_buddy.runner.pnr_results import PnrPassResults
from rtl_buddy.tools import pnr_abstract
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

from test_multi_corner import _pnr_backend_over
from test_pnr import _make_pdk_cfg, _make_pnr_cfg, _platform, _render_flow
from tcl_eval import _tcl_eval

_BUFFERS = ["BUF_X1", "BUF_X4", "BUF_X8"]


def _limits(**overrides) -> PlacementFile:
    return PlacementFile(macro_pin_limits=MacroPinLimitsFile(**overrides))


def _on(tmp_path, **overrides):
    base = {"buffers": list(_BUFFERS)}
    base.update(overrides)
    return _platform(_make_pdk_cfg(tmp_path), placement=_limits(**base))


# --- configuration ------------------------------------------------------------


def test_the_step_is_off_by_default(tmp_path):
    assert _platform(_make_pdk_cfg(tmp_path)).get_placement_macro_pin_limits() is None


def test_yaml_key_loads_with_the_default_margin(tmp_path):
    platform = PnrPlatformConfig(
        from_yaml(
            PnrPlatformConfigFile,
            dedent("""\
                name: "p"
                pdk: "p"
                placement:
                  macro-pin-limits:
                    buffers: [BUF_X1, BUF_X8]
            """),
        ),
        lambda _n: _make_pdk_cfg(tmp_path),
    )
    limits = platform.get_placement_macro_pin_limits()
    assert limits.buffers == ["BUF_X1", "BUF_X8"]
    assert limits.margin == 0.05


def test_an_integer_margin_loads_as_a_float(tmp_path):
    assert _on(tmp_path, margin=0).get_placement_macro_pin_limits().margin == 0.0


def test_the_capacitance_margin_defaults_to_the_margin(tmp_path):
    limits = _on(tmp_path, margin=0.1).get_placement_macro_pin_limits()
    assert limits.capacitance_margin == 0.1
    limits = _on(tmp_path, capacitance_margin=0.5).get_placement_macro_pin_limits()
    assert (limits.margin, limits.capacitance_margin) == (0.05, 0.5)


def test_yaml_capacitance_margin_key_loads(tmp_path):
    platform = PnrPlatformConfig(
        from_yaml(
            PnrPlatformConfigFile,
            'name: "p"\npdk: "p"\nplacement:\n  macro-pin-limits:\n'
            "    buffers: [BUF_X1]\n    capacitance-margin: 0.5\n",
        ),
        lambda _n: _make_pdk_cfg(tmp_path),
    )
    assert platform.get_placement_macro_pin_limits().capacitance_margin == 0.5


def test_the_pdk_block_applies_and_a_platform_block_replaces_it_whole(tmp_path):
    pdk = _make_pdk_cfg(
        tmp_path, placement=_limits(buffers=["BUF_X1", "BUF_X8"], margin=0.1)
    )
    assert _platform(pdk).get_placement_macro_pin_limits().margin == 0.1

    replaced = _platform(pdk, placement=_limits(buffers=["BUF_X16"]))
    limits = replaced.get_placement_macro_pin_limits()
    assert limits.buffers == ["BUF_X16"]
    # Not inherited from the PDK's block: the platform's block is complete.
    assert limits.margin == 0.05


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"buffers": None}, "buffers must be a non-empty list"),
        ({"buffers": []}, "buffers must be a non-empty list"),
        ({"buffers": "BUF_X1"}, "buffers must be a non-empty list"),
        ({"buffers": ["BUF_X1 BUF_X2"]}, "entries must be single cell names"),
        ({"buffers": [""]}, "entries must be single cell names"),
        ({"buffers": [3]}, "entries must be single cell names"),
        ({"buffers": ["BUF[1]"]}, r"contains \[ \]"),
        ({"buffers": ["BUF_X1", "BUF_X1"]}, "repeats a cell"),
        ({"buffers": ["BUF_X1"], "margin": 1}, r"margin must be >= 0 and < 1"),
        ({"buffers": ["BUF_X1"], "margin": 1.5}, r"margin must be >= 0 and < 1"),
        ({"buffers": ["BUF_X1"], "margin": -0.01}, r"margin must be >= 0 and < 1"),
        ({"buffers": ["BUF_X1"], "margin": True}, "margin must be a number"),
        ({"buffers": ["BUF_X1"], "margin": "0.05"}, "margin must be a number"),
        (
            {"buffers": ["BUF_X1"], "capacitance_margin": 1.0},
            r"capacitance-margin must be >= 0 and < 1",
        ),
        (
            {"buffers": ["BUF_X1"], "capacitance_margin": False},
            "capacitance-margin must be a number",
        ),
    ],
)
def test_a_malformed_block_is_refused_naming_the_key(tmp_path, overrides, message):
    with pytest.raises(FatalRtlBuddyError, match=message) as excinfo:
        _platform(_make_pdk_cfg(tmp_path), placement=_limits(**overrides))
    assert "pnr platform 'nangate45_typ': placement.macro-pin-limits" in str(
        excinfo.value
    )


def test_a_malformed_pdk_block_names_the_pdk(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="PDK 'nangate45': placement.macro"):
        _make_pdk_cfg(tmp_path, placement=_limits(buffers=[]))


def test_the_block_joins_the_hardened_config_digest_only_when_set(tmp_path):
    pnr_cfg = _make_pnr_cfg(tmp_path, harden=True)
    plain = pnr_abstract.abstract_config(pnr_cfg, _platform(_make_pdk_cfg(tmp_path)))
    assert "macro_pin_limits" not in plain["placement"]

    on = pnr_abstract.abstract_config(pnr_cfg, _on(tmp_path, margin=0.1))
    assert on["placement"]["macro_pin_limits"] == {
        "buffers": _BUFFERS,
        "margin": 0.1,
        "capacitance_margin": 0.1,
    }
    assert pnr_abstract.config_digest(on) != pnr_abstract.config_digest(plain)


# --- rendered flow ------------------------------------------------------------

_LEGALIZED = 'puts ">>> Detail placement (legalize)"\ndetailed_placement\n'
_OPTIMIZED = (
    'puts ">>> Detail placement optimization"\nimprove_placement\noptimize_mirroring\n'
)
_PLACE_END = 'rb::global_connect\n\nputs ">>> Clock tree synthesis"'
_CTS = 'puts ">>> Clock tree synthesis"'


def _block(tmp_path, platform, dpl_options="") -> str:
    from rtl_buddy.tools.pnr_openroad import _macro_pin_limits_tcl

    return _macro_pin_limits_tcl(platform, dpl_options)


def test_an_unset_key_renders_the_flow_byte_identically(tmp_path):
    """The block is the only difference, so a run without the key keeps its pnr.tcl."""
    off = _render_flow(tmp_path, _platform(_make_pdk_cfg(tmp_path)))
    on_platform = _on(tmp_path)
    on = _render_flow(tmp_path, on_platform)
    block = _block(tmp_path, on_platform)

    assert block and block in on
    assert on.replace(block, "", 1) == off
    assert "rb::macro_pins" not in off
    assert "Macro pin limits" not in off
    assert f"{_LEGALIZED}{_OPTIMIZED}{_PLACE_END}" in off


def test_the_step_runs_on_the_legalised_post_repair_placement_before_cts(tmp_path):
    """After `repair_design`, its hook, its legalisation and the placement optimisation, at the end of the place
    stage: legalisation moves a macro output's loads, so checks read before it miss pins.
    """
    platform = _on(tmp_path, margin=0.1)
    text = _render_flow(tmp_path, platform)
    block = _block(tmp_path, platform)

    assert f"repair_design\n\n{_LEGALIZED}{_OPTIMIZED}{block}{_PLACE_END}" in text
    assert text.count('puts ">>> Macro pin limits"') == 1
    assert text.index("global_placement -density") < text.index(block)
    assert text.index(block) < text.index("clock_tree_synthesis")
    # Buffers weakest first, the two margins, the global-placement padding, the tag
    # and the flow's legalizer options.
    assert block.endswith(
        "rb::macro_pins::repair {BUF_X1 BUF_X4 BUF_X8} 0.1 0.1 0 "
        "RB-MACRO-PIN-LIMITS: [list]\n"
    )
    assert _block(tmp_path, _on(tmp_path, capacitance_margin=0.5)).endswith(
        "{BUF_X1 BUF_X4 BUF_X8} 0.05 0.5 0 RB-MACRO-PIN-LIMITS: [list]\n"
    )


def test_the_placement_padding_reaches_the_site_search(tmp_path):
    pdk = _make_pdk_cfg(tmp_path)
    platform = _platform(
        pdk, placement=PlacementFile(padding=3, macro_pin_limits=_BLOCK)
    )
    assert "{BUF_X1} 0.05 0.05 3 RB-MACRO-PIN-LIMITS:" in _block(tmp_path, platform)


def test_the_step_legalizes_with_the_flow_s_detailed_placement_options(tmp_path):
    """With `rotated-macro-footprint: diamond-legalizer` every `detailed_placement`
    of the flow takes `-use_diamond_legalizer`, the step's own included.
    """
    block = _block(tmp_path, _on(tmp_path), " {*}$RB_DPL_OPTIONS")
    assert block.endswith(" RB-MACRO-PIN-LIMITS: [list {*}$RB_DPL_OPTIONS]\n")
    assert "\n    detailed_placement {*}$dpl_args\n" in _repair_body(block)


def test_a_run_placement_block_replaces_the_platform_s_whole(tmp_path):
    platform = _on(tmp_path, margin=0.1)
    run = platform.with_run_placement(
        validate_placement(_limits(buffers=["BUF_X16"]), "pnr run 'demo'"), "demo"
    )
    limits = run.get_placement_macro_pin_limits()
    assert (limits.buffers, limits.margin) == (["BUF_X16"], 0.05)
    # A run block without the key keeps the platform's.
    kept = platform.with_run_placement(PlacementFile(density=0.5), "demo")
    assert kept.get_placement_macro_pin_limits().margin == 0.1


_BLOCK = MacroPinLimitsFile(buffers=["BUF_X1"])


def _repair_body(block: str) -> str:
    start = block.index("proc rb::macro_pins::repair")
    return block[start:]


def test_supplies_are_connected_before_dont_touch_in_the_documented_order(tmp_path):
    """global_connect skips dont_touch instances (ODB-0383), so it must come first;
    then legalize, estimate, size weakest first and only then set dont_touch.
    """
    body = _repair_body(_block(tmp_path, _on(tmp_path)))
    estimate = "\n    estimate_parasitics -placement\n"
    order = [
        body.index(estimate),
        body.index("set before [measure $pin]"),
        body.index("insert_buffer -load_pins"),
        body.index("\n    ::rb::global_connect\n"),
        body.index("\n    detailed_placement {*}$dpl_args\n"),
        body.rindex(estimate),
        body.index("replace_cell $final $final_cell"),
        body.rindex("\n    ::rb::global_connect\n"),
        body.index("set_dont_touch $dont_touch"),
    ]
    assert order == sorted(order)
    assert "setPlacementStatus FIRM" in body
    # The macro's checks come from the timer, in the macro library's thresholds.
    assert "report_check_types -max_slew -max_capacitance -net $net" in _block(
        tmp_path, _on(tmp_path)
    )
    assert "sta::is_clock $pin" in body


def test_the_rendered_block_has_no_template_placeholder(tmp_path):
    block = _block(tmp_path, _on(tmp_path))
    assert "{{" not in block and "}}" not in block


# --- pure Tcl helpers ---------------------------------------------------------

# `report_check_types -max_slew -max_capacitance -net <net> -digits 6`, abridged
# from an OpenROAD 26Q2 run on the sky130hd OpenRAM SRAM.
_REPORT = """\
max slew

Pin                                      Limit      Slew     Slack
------------------------------------------------------------------
u_inner/u_hist_mem/u_sram/addr0[2]      0.040000    0.143900   -0.103900 (VIOLATED)
u_inner/u_hist_mem/wire145/X            1.506500    0.107900    1.398500 (MET)

max capacitance

Pin                                      Limit       Cap     Slack
------------------------------------------------------------------
u_inner/u_hist_mem/wire145/X            0.130000    0.007600    0.122400 (MET)
u_inner/u_hist_mem/u_sram/dout1[13]     0.027560    0.080000   -0.052440 (VIOLATED)

"""


def _run(body: str) -> str:
    source = files("rtl_buddy.pnr").joinpath("macro_pin_limits.tcl").read_text()
    return _tcl_eval(source + "\n" + body + "\n")


def _tcl_braced(text: str) -> str:
    return "{" + text + "}"


# One line per parsed check: kind, pin, limit, value.
_ROWS_OUT = (
    "set lines {}\n"
    "foreach kind {slew capacitance} {\n"
    "    foreach row [dict get $c $kind] { lappend lines [join [linsert $row 0 $kind]] }\n"
    "}\n"
    'set result [join $lines "\\n"]'
)


def test_parse_checks_reads_both_tables_in_report_units():
    out = _run(
        f"set c [rb::macro_pins::parse_checks {_tcl_braced(_REPORT)}]\n" + _ROWS_OUT
    )
    assert out.splitlines() == [
        "slew u_inner/u_hist_mem/u_sram/addr0[2] 0.040000 0.143900",
        "slew u_inner/u_hist_mem/wire145/X 1.506500 0.107900",
        "capacitance u_inner/u_hist_mem/wire145/X 0.130000 0.007600",
        "capacitance u_inner/u_hist_mem/u_sram/dout1[13] 0.027560 0.080000",
    ]


def test_parse_checks_tolerates_a_scene_column():
    report = (
        "max slew\n\nPin   Corner   Limit   Slew   Slack\n------\n"
        "u_sram/addr0[0]   ss   0.04   0.05   -0.01 (VIOLATED)\n"
    )
    out = _run(
        f"set c [rb::macro_pins::parse_checks {_tcl_braced(report)}]\n" + _ROWS_OUT
    )
    assert out.splitlines() == ["slew u_sram/addr0[0] 0.04 0.05"]


def test_pin_check_takes_the_named_pin_worst_headroom():
    report = _REPORT + (
        "max slew\n\n"
        "u_inner/u_hist_mem/u_sram/addr0[2]      0.050000    0.149000   "
        "-0.099000 (VIOLATED)\n"
    )
    out = _run(
        f"set c [rb::macro_pins::parse_checks {_tcl_braced(report)}]\n"
        "set result [list "
        "[rb::macro_pins::pin_check $c slew {u_inner/u_hist_mem/u_sram/addr0[2]}] "
        "[rb::macro_pins::pin_check $c capacitance {u_inner/u_hist_mem/u_sram/addr0[2]}]]"
    )
    assert out == "{0.040000 0.143900} {}"


@pytest.mark.parametrize(
    ("value", "limit", "margin", "expected"),
    [
        ("0.039", "0.04", "0.05", "1"),
        ("0.037", "0.04", "0.05", "0"),
        ("0.04", "0.04", "0", "0"),
        ("0.0401", "0.04", "0", "1"),
    ],
)
def test_over_keeps_the_margin_in_hand(value, limit, margin, expected):
    assert _run(f"set result [rb::macro_pins::over {value} {limit} {margin}]") == (
        expected
    )


def test_fits_judges_inputs_on_slew_and_outputs_on_slew_and_capacitance():
    measured = "{0.5 0.01 0.0276 0.08}"
    out = _run(
        "set result [list "
        f"[rb::macro_pins::fits {measured} slew {{0.05 0.05}}] "
        f"[rb::macro_pins::fits {measured} {{slew capacitance}} {{0.05 0.05}}] "
        "[rb::macro_pins::fits {0.04 0.03 {} {}} {slew capacitance} {0.05 0.05}] "
        # The capacitance margin is its own: 0.02 of 0.0276 is over 0.0276 less 30%.
        "[rb::macro_pins::fits {0.5 0.01 0.0276 0.02} {slew capacitance} {0.05 0.05}] "
        "[rb::macro_pins::fits {0.5 0.01 0.0276 0.02} {slew capacitance} {0.05 0.3}]]"
    )
    assert out == "1 0 1 1 0"


# One row 0..10000 DBU high 0..100, site pitch 10; the target sits on the row.
_ROW = "{{0 0 10000 100 10 R0}}"


def _site(blockers: str, target: str, width: int, pad: int, rows: str = _ROW):
    return _run(
        f"set result [rb::macro_pins::nearest_site {rows} {blockers} "
        f"{target} {width} {pad}]"
    )


def test_an_open_row_takes_the_site_centred_on_the_target():
    assert _site("{}", "{5000 50}", 40, 0) == "4980 0 R0"


def test_the_site_stays_outside_a_hard_blockage_plus_the_padding():
    # A macro halo blockage covering x 4000..6000 over the row.
    assert _site("{{4000 -100 6000 200}}", "{5100 50}", 40, 10) == "6010 0 R0"
    assert _site("{{4000 -100 6000 200}}", "{4900 50}", 40, 10) == "3950 0 R0"


def test_two_padded_buffers_keep_both_paddings_between_them():
    # A buffer already fixed at 5000..5040 blocks 4990..5050 with its padding;
    # the next one's own padding keeps another site free.
    assert _site("{{4990 0 5050 100}}", "{5030 50}", 40, 10) == "5060 0 R0"


def test_the_padded_cell_stays_inside_its_row():
    assert _site("{}", "{0 50}", 40, 10) == "10 0 R0"
    assert _site("{}", "{10000 50}", 40, 10) == "9950 0 R0"


def test_the_nearest_row_wins_and_carries_its_orientation():
    rows = "{{0 0 1000 100 10 R0} {0 100 1000 200 10 MX} {0 200 1000 300 10 R0}}"
    # Row 0 blocked entirely; row 1 is the next nearest to a target at y=40.
    assert _site("{{0 0 1000 100}}", "{500 40}", 40, 0, rows) == "480 100 MX"


@pytest.mark.parametrize(
    ("width", "px", "expected"),
    [
        # A slot reserved at 1000..1200 for a 200-wide cell; a 60-wide cell slides
        # to the pin's end of it and never leaves it.
        (60, 900, "1000"),
        (60, 1300, "1140"),
        (60, 1100, "1070"),
        (200, 1300, "1000"),
    ],
)
def test_a_narrower_final_buffer_slides_toward_the_pin_within_its_slot(
    width, px, expected
):
    assert _run(f"set result [rb::macro_pins::slot_x 1000 200 {width} {px} 10]") == (
        expected
    )


def test_a_fully_blocked_floorplan_has_no_site():
    assert _site("{{0 0 10000 100}}", "{5000 50}", 40, 0) == ""


# --- log parsing and results --------------------------------------------------


def test_the_count_line_is_parsed_from_the_log(tmp_path):
    backend = OpenRoadPnr("demo/openroad", _make_pnr_cfg(tmp_path), str(tmp_path), None)
    log = (
        ">>> Macro pin limits\n"
        ">>>   u_sram/addr0[2] (input): slew 0.1439 ns (limit 0.0400) -> ...\n"
        "RB-MACRO-PIN-LIMITS: buffered 9 unmet 1\n"
    )
    assert backend._macro_pin_limits_fields(log) == {
        "macro_pin_buffered_count": 9,
        "macro_pin_unmet_count": 1,
    }
    # The step did not run: no fields, rather than zeros.
    assert backend._macro_pin_limits_fields("RB-ELECTRICAL: max_slew 0\n") == {}


_RAN = (
    "RB-MACRO-PIN-LIMITS: buffered 8 unmet {unmet}\n"
    "worst slack max 1.0\n"
    "RB-ELECTRICAL: max_slew 0\n"
    "RB-ELECTRICAL: max_capacitance 0\n"
    "RB-ELECTRICAL: max_fanout 0\n"
)


def test_a_run_records_the_counts_and_warns_on_unmet_pins(
    tmp_path, monkeypatch, caplog
):
    platform = _on(tmp_path)
    with caplog.at_level(logging.WARNING):
        res = _pnr_backend_over(
            tmp_path, monkeypatch, platform, _RAN.format(unmet=2)
        ).run()

    assert isinstance(res, PnrPassResults)
    assert res.results["macro_pin_buffered_count"] == 8
    assert res.results["macro_pin_unmet_count"] == 2
    assert any(
        "2 macro pin(s) stayed over their max-slew or max-capacitance limit"
        in r.getMessage()
        for r in caplog.records
    )


def test_a_run_with_every_pin_met_does_not_warn(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        res = _pnr_backend_over(
            tmp_path, monkeypatch, _on(tmp_path), _RAN.format(unmet=0)
        ).run()
    assert res.results["macro_pin_unmet_count"] == 0
    assert not any("macro pin" in r.getMessage() for r in caplog.records)


def test_the_machine_row_carries_the_counts():
    from rtl_buddy.rtl_buddy import RtlBuddy

    results = PnrPassResults(
        name="demo/results",
        fields={"macro_pin_buffered_count": 9, "macro_pin_unmet_count": 0},
    )
    row = RtlBuddy._pnr_result_row(None, {"pnr_name": "demo", "results": results})
    assert row["macro_pin_buffered_count"] == 9
    assert row["macro_pin_unmet_count"] == 0


def test_the_unmet_warning_has_a_human_message():
    message = _human_message(
        "pnr.macro_pin_limits_unmet",
        {"pnr": "blk", "unmet": 3, "buffered": 9, "log": "pnr.log"},
    )
    assert message.startswith("pnr blk: 3 macro pin(s) stayed over")
    assert "pnr.log" in message


# --- staged runs ----------------------------------------------------------------


def test_the_step_belongs_to_the_place_stage_of_a_staged_run(tmp_path):
    """Its cells and dont_touch marks are in the place checkpoint, which CTS reads."""
    from test_pnr_checkpoints import _pnr_cfg as _ckpt_pnr_cfg
    from test_pnr_checkpoints import _render as _ckpt_render

    from rtl_buddy.config.pnr import CHECKPOINT_STAGES
    from rtl_buddy.tools import pnr_checkpoints, pnr_stages

    run_dir = tmp_path / "ck" / "run"
    run_dir.mkdir(parents=True)
    _ckpt_render(
        tmp_path,
        CHECKPOINT_STAGES,
        pnr_cfg=_ckpt_pnr_cfg(tmp_path, stages=True),
        placement=_limits(buffers=list(_BUFFERS)),
    )
    scripts = {
        stage: (
            run_dir / f"{pnr_checkpoints.STAGE_INDEX[stage]}_{stage}.tcl"
        ).read_text()
        for stage in pnr_stages.TEMPLATE_STAGES
    }
    call = "rb::macro_pins::repair {BUF_X1 BUF_X4 BUF_X8} "
    assert [s for s, text in scripts.items() if call in text] == ["place"]
    place = scripts["place"]
    assert place.index("repair_design\n") < place.index(call)
    assert place.index(call) < place.index("rb::ckpt::stage_end place")


def _stubbed_step(monkeypatch, buffered=3, unmet=1):
    """Run the step's real block under the stub OpenROAD, its OpenROAD half replaced by the count line."""
    import re

    from rtl_buddy.tools import pnr_openroad

    real = pnr_openroad._macro_pin_limits_tcl

    def _block(platform, dpl_options=""):
        return re.sub(
            r"^rb::macro_pins::repair .*$",
            f'puts "RB-MACRO-PIN-LIMITS: buffered {buffered} unmet {unmet}"',
            real(platform, dpl_options),
            flags=re.M,
        )

    monkeypatch.setattr(pnr_openroad, "_macro_pin_limits_tcl", _block)


def _staged_backend(tmp_path, monkeypatch, **options):
    from test_pnr_checkpoints import _backend

    return _backend(
        tmp_path,
        monkeypatch,
        stages=True,
        pdk_fields={"placement": _limits(buffers=["BUF_X1"])},
        **options,
    )


def test_a_staged_run_reports_the_counts_of_its_place_session(
    tmp_path, monkeypatch, caplog
):
    from test_pnr_checkpoints import _runs

    _stubbed_step(monkeypatch)
    backend, artefacts = _staged_backend(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING):
        res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["macro_pin_buffered_count"] == 3
    assert res.results["macro_pin_unmet_count"] == 1
    assert any("1 macro pin(s) stayed over" in r.getMessage() for r in caplog.records)
    (run_dir,) = _runs(artefacts)
    logs = [
        p.name for p in sorted(run_dir.glob("*.log")) if "RB-MACRO" in p.read_text()
    ]
    assert logs == ["02_place.log"]


def test_a_run_stopped_after_place_reports_the_counts(tmp_path, monkeypatch):
    _stubbed_step(monkeypatch, unmet=0)
    backend, _ = _staged_backend(tmp_path, monkeypatch, stop_after="place")

    res = backend.run()

    assert res.results["stopped_after"] == "place", res.results
    assert res.results["macro_pin_buffered_count"] == 3
    assert res.results["macro_pin_unmet_count"] == 0


def test_a_run_resumed_after_place_reports_the_inherited_counts(tmp_path, monkeypatch):
    from test_pnr_checkpoints import _runs

    _stubbed_step(monkeypatch)
    backend, artefacts = _staged_backend(tmp_path, monkeypatch, stop_after="cts")
    assert backend.run().results["result"] == "PASS"
    (first,) = _runs(artefacts)

    backend, _ = _staged_backend(
        tmp_path, monkeypatch, resume=first.name, resume_from="global_route"
    )
    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["macro_pin_buffered_count"] == 3
    (second,) = [r for r in _runs(artefacts) if r != first]
    assert not (second / "02_place.log").exists()
