"""`icg_split` deals the loads of an over-full clock-gating cell across copies of it (#854)."""

import json
import subprocess
import sys

from rtl_buddy.tools import icg_split

ICG = "$auto$clockgate.cc:372:execute$10"


def _module(loads, *, icg=ICG, extra_cells=None):
    """One ICG (CK=2, E=3, GCK=4) driving ``loads`` flops, flop i named f"ff{i:03d}"."""
    cells = {
        icg: {
            "type": "CLKGATE_X1",
            "parameters": {},
            "attributes": {"src": "t.v:1"},
            "port_directions": {"CK": "input", "E": "input", "GCK": "output"},
            "connections": {"CK": [2], "E": [3], "GCK": [4]},
        }
    }
    for i in range(loads):
        cells[f"ff{i:03d}"] = {
            "type": "$_DFF_P_",
            "port_directions": {"C": "input", "D": "input", "Q": "output"},
            "connections": {"C": [4], "D": [100 + i], "Q": [400 + i]},
        }
    cells.update(extra_cells or {})
    return {"ports": {}, "cells": cells, "netnames": {}}


def _gclk_of(module, cell):
    return module["cells"][cell]["connections"]["C"][0]


def test_shares_are_near_equal_and_within_the_limit():
    assert icg_split._shares(40, 16) == [14, 13, 13]
    assert icg_split._shares(32, 16) == [16, 16]
    assert icg_split._shares(17, 16) == [9, 8]


def test_an_over_full_icg_is_split_in_name_order():
    module = _module(40)
    assert icg_split.split_module(module, 16) == [(ICG, 40, 3)]
    cells = module["cells"]
    copies = [f"{ICG}$split1", f"{ICG}$split2"]
    for name in copies:
        twin = cells[name]
        assert twin["type"] == "CLKGATE_X1"
        assert twin["attributes"] == {"src": "t.v:1"}
        assert twin["connections"]["CK"] == [2]
        assert twin["connections"]["E"] == [3]
    nets = [4, *(cells[name]["connections"]["GCK"][0] for name in copies)]
    assert len(set(nets)) == 3
    assert [_gclk_of(module, f"ff{i:03d}") for i in range(40)] == (
        [nets[0]] * 14 + [nets[1]] * 13 + [nets[2]] * 13
    )
    for name in copies:
        assert (
            module["netnames"][f"{name}$gclk"]["bits"]
            == cells[name]["connections"]["GCK"]
        )


def test_new_nets_do_not_reuse_an_existing_bit():
    module = _module(20)
    module["netnames"]["wide"] = {"bits": [9000], "hide_name": 0, "attributes": {}}
    icg_split.split_module(module, 16)
    assert module["cells"][f"{ICG}$split1"]["connections"]["GCK"] == [9001]


def test_groups_within_the_limit_and_other_cells_are_left_alone():
    user_icg = {
        "type": "CLKGATE_X1",
        "port_directions": {"CK": "input", "E": "input", "GCK": "output"},
        "connections": {"CK": [2], "E": [5], "GCK": [6]},
    }
    module = _module(16, extra_cells={"u_icg": user_icg})
    for i in range(30):
        module["cells"][f"g{i:02d}"] = {
            "type": "$_DFF_P_",
            "port_directions": {"C": "input", "D": "input", "Q": "output"},
            "connections": {"C": [6], "D": [700 + i], "Q": [800 + i]},
        }
    before = json.dumps(module, sort_keys=True)
    assert icg_split.split_module(module, 16) == []
    assert json.dumps(module, sort_keys=True) == before


def test_the_script_rewrites_a_design_file(tmp_path):
    source = tmp_path / "in.json"
    target = tmp_path / "out.json"
    source.write_text(json.dumps({"modules": {"top": _module(33)}}))
    run = subprocess.run(
        [sys.executable, "-I", icg_split.__file__, str(source), str(target), "16"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert run.stdout == f"icg_split: top {ICG}: 33 loads over 3 cells\n"
    assert not source.exists()
    cells = json.loads(target.read_text())["modules"]["top"]["cells"]
    assert sorted(name for name in cells if name.startswith(ICG)) == [
        ICG,
        f"{ICG}$split1",
        f"{ICG}$split2",
    ]
