"""Tests for the power hookup of `blocks:` instances (rtl_buddy#713).

`block_power.tcl` ties each block supply pin the pdn-config leaves floating to a parent
supply net, and joins a block's supply pins on the parent's top strap layer to the
parent's straps, which `pdngen` stops short of the block. Its geometry is pure Tcl and is
driven here without OpenROAD; the rendering and the failure report go through `rb pnr`.
"""

from importlib.resources import files
from pathlib import Path

from rtl_buddy.config.pdk import PdkConfig
from rtl_buddy.tools import pnr_openroad

from test_pnr_blocks import _BLOCK_YAML, _block_suite, _top_backend, _write
from test_pnr_macro_pack import _tcl_eval


def _tcl(body: str) -> str:
    source = files("rtl_buddy.pnr").joinpath("block_power.tcl").read_text()
    return _tcl_eval(source + "\n" + body + "\n")


def _plan(pin, own, others=(), horizontal=True):
    def rect(r):
        return "{" + " ".join(str(v) for v in r) + "}"

    own_tcl = " ".join(rect(r) for r in own)
    others_tcl = " ".join("{" + f"{name} {rect(r)}" + "}" for name, r in others)
    return _tcl(
        f"set result [rb::block_power::plan_join {rect(pin)} "
        f"{1 if horizontal else 0} {{{own_tcl}}} {{{others_tcl}}}]"
    )


# The issue's geometry in DBU: a 1.6 um met5 pin across the block, and the parent's
# straps on its track ending 3.2 um short of it on both sides.
_PIN = (35880, 64480, 92000, 66080)
_LEFT = (10120, 64480, 32680, 66080)
_RIGHT = (95200, 64480, 230460, 66080)


def test_a_pin_between_two_strap_ends_is_joined_overlapping_each_by_a_width():
    assert _plan(_PIN, [_LEFT, _RIGHT]) == "join {31080 64480 96800 66080}"


def test_the_join_comes_from_the_strap_gap_not_a_fixed_overlap():
    left = (10120, 64480, 30000, 66080)
    right = (99000, 64480, 230460, 66080)
    assert _plan(_PIN, [left, right]) == "join {28400 64480 100600 66080}"


def test_a_pin_with_a_strap_on_one_side_only_is_joined_to_that_side():
    assert _plan(_PIN, [_LEFT]) == "join {31080 64480 92000 66080}"


def test_straps_on_other_tracks_are_ignored():
    other_track = (95200, 37280, 230460, 38880)
    assert _plan(_PIN, [_LEFT, other_track]) == "join {31080 64480 92000 66080}"


def test_a_pin_a_strap_already_overlaps_needs_no_join():
    through = (10120, 64480, 230460, 66080)
    assert _plan(_PIN, [through]) == "covered"


def test_a_pin_on_the_other_nets_track_fails_the_phase_check():
    """A mirrored (MX) copy puts each supply pin where the other net's strap runs."""
    own_elsewhere = (10120, 50880, 32680, 52480)
    result = _plan(_PIN, [own_elsewhere], others=[("VSS", _LEFT)])
    assert result == "misaligned VSS 51680"


def test_a_pin_on_no_track_fails_the_phase_check():
    assert _plan(_PIN, [], others=[]) == "misaligned {} {}"


def test_a_vertical_layer_joins_along_y():
    pin = (64480, 35880, 66080, 92000)
    below = (64480, 10120, 66080, 32680)
    above = (64480, 95200, 66080, 230460)
    assert (
        _plan(pin, [below, above], horizontal=False) == "join {64480 31080 66080 96800}"
    )


def test_a_pin_ties_to_its_namesake_else_the_only_net_of_its_kind():
    assert _tcl("set result [rb::block_power::pick_net VDD {VDD VDDA}]") == "VDD"
    assert _tcl("set result [rb::block_power::pick_net VDDB {VDD}]") == "VDD"
    assert _tcl("set result [rb::block_power::pick_net VDDB {VDD VDDA}]") == ""
    assert _tcl("set result [rb::block_power::pick_net VDDB {}]") == ""


def test_instance_names_are_quoted_for_the_global_connection_pattern():
    assert (
        _tcl(r"set result [rb::block_power::regex_quote {u_top.blk[0]$x}]")
        == r"u_top\.blk\[0\]\$x"
    )


def _with_pdn_config(tmp_path, monkeypatch):
    pdn = _write(tmp_path / "top/pdn.tcl", "# grid\n")
    monkeypatch.setattr(PdkConfig, "get_pdn_config", lambda self: str(pdn))
    return pdn


def test_a_run_with_blocks_ties_before_pdngen_and_joins_after(tmp_path, monkeypatch):
    _block_suite(tmp_path)
    pdn = _with_pdn_config(tmp_path, monkeypatch)
    backend, _ = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)

    assert backend.run().is_pass()

    script = Path(backend._script_path()).read_text()
    source = script.index(f"source {pdn}\n")
    tie = script.index("rb::block_power::tie_supplies {blk_top}\n")
    pdngen = script.index("\npdngen\n")
    join = script.index("rb::block_power::join_straps {blk_top}\n")
    assert source < tie < pdngen < join
    assert script.count("proc rb::block_power::plan_join") == 1


def test_a_run_without_blocks_keeps_the_plain_power_grid(tmp_path, monkeypatch):
    pdn = _with_pdn_config(tmp_path, monkeypatch)
    backend, _ = _top_backend(tmp_path, monkeypatch, "")

    assert backend.run().is_pass()

    script = Path(backend._script_path()).read_text()
    assert f"source {pdn}\npdngen\n" in script
    assert "block_power" not in script


def test_a_block_power_problem_fails_the_run_naming_the_pin(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    _block_suite(tmp_path)
    _with_pdn_config(tmp_path, monkeypatch)
    backend, _ = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)
    problems = [
        "u_blk/VDDB (VDD, met5 y=68.000 um, R0) is not on a parent VDD strap",
        "u_blk/VSSB (VSS, met5 y=54.400 um, R0) is not on a parent VSS strap",
    ]

    def _run(cmd, **_kw):
        Path(cmd[cmd.index("-log") + 1]).write_text(
            "".join(f"RB-BLOCK-POWER-ERROR: {p}\n" for p in problems)
        )
        return MagicMock(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["desc"] == f"block power: {problems[0]} (+1 more)"


def test_a_stray_off_track_shape_is_a_warning_not_a_failure(
    tmp_path, monkeypatch, caplog
):
    from unittest.mock import MagicMock

    _block_suite(tmp_path)
    _with_pdn_config(tmp_path, monkeypatch)
    backend, _ = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)
    stray = "u_blk/VDD (VDD, met5 y=44.540 um, R0) is not on a parent VDD strap"

    def _run(cmd, **_kw):
        Path(cmd[cmd.index("-log") + 1]).write_text(
            f"RB-BLOCK-POWER-WARNING: {stray}; the pin's other shapes are joined\n"
        )
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)

    with caplog.at_level("WARNING"):
        assert backend.run().is_pass()
    assert any(stray in r.getMessage() for r in caplog.records)
