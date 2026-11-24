"""Tests for a top left without rows, such as an assembly whose hardened blocks cover the core.

With no standard cell to place the run skips the steps that need rows, still ends every stage with its checkpoint, routes, and qualifies its pass; with standard cells it stops at the end of the floorplan. The generated `pnr.tcl` runs under the stub OpenROAD of `test_pnr_checkpoints`, whose core has a row unless `RB_STUB_NO_ROWS` is set.
"""

import logging

import pytest
from pnr_events import _capture_pnr_events, _one_event
from pnr_stub_openroad import _HARNESS, _backend, _events, _runs

from rtl_buddy.logging_utils import _human_message
from rtl_buddy.runner.pnr_results import PnrFailResults, PnrPassResults
from rtl_buddy.tools.pnr_stages import TEMPLATE_STAGES

#: The traced flow commands that place, legalize, buffer or fill cells. `repair_timing` runs once, as the floorplan's setup repair, which sizes cells before the rows are cut.
_ROW_STEPS = (
    "global_placement",
    "repair_tie_fanout",
    "repair_design",
    "detailed_placement",
    "improve_placement",
    "optimize_mirroring",
    "repair_clock_inverters",
    "clock_tree_synthesis",
    "repair_clock_nets",
    "check_placement",
    "repair_antennas",
    "filler_placement",
)


@pytest.mark.parametrize("stages", [False, True], ids=["one session", "staged"])
def test_a_top_with_nothing_to_place_skips_the_steps_that_need_rows(
    tmp_path, monkeypatch, stages
):
    """Without rows and with every instance a fixed block, the run routes and passes; no step that needs rows runs, every stage still writes its checkpoint, and the result says what was skipped."""
    backend, artefacts = _backend(
        tmp_path, monkeypatch, env={"RB_STUB_NO_ROWS": "1"}, stages=stages
    )
    events = _capture_pnr_events(monkeypatch)

    res = backend.run()

    assert isinstance(res, PnrPassResults), res.results
    assert "pnr.no_rows_skip_placement" in res.results["desc"]
    assert res.results["checkpoint_stages"] == list(TEMPLATE_STAGES[:-1])
    (run_dir,) = _runs(artefacts)
    steps = [e["step"] for e in _events(run_dir) if e["event"] == "step_begin"]
    assert not set(steps) & set(_ROW_STEPS), sorted(set(steps) & set(_ROW_STEPS))
    assert steps.count("repair_timing") == 1
    assert steps.index("repair_timing") < steps.index("place_pins")
    assert {"global_route", "detailed_route", "check_antennas"} <= set(steps)
    log = (artefacts / "pnr.log").read_text().splitlines()
    assert ">>> rb: no rows: global placement, resize and legalization skipped" in log
    assert ">>> rb: no rows: fill insertion skipped" in log
    level, fields = _one_event(events, "pnr.no_rows_skip_placement")
    assert level == logging.WARNING
    assert _human_message("pnr.no_rows_skip_placement", fields) == (
        'P&R "demo_pnr": the core has no rows and the top no standard cell to '
        "place, so placement, CTS, the post-CTS and global-route repairs, antenna "
        "diode insertion and fill did not run"
    )


#: A block holding one standard cell that is not fixed, `u_buf`, and no rows.
_UNPLACED_CELL = r"""
namespace eval ord {}
proc ord::get_db_block {} { return rb_blk }
proc rb_blk {cmd args} {
    switch $cmd {
        getInsts { return {rb_buf} }
        default { return "" }
    }
}
proc rb_buf {cmd args} {
    switch $cmd {
        getMaster { return rb_buf_master }
        getName { return u_buf }
        isFixed { return 0 }
        default { return "" }
    }
}
proc rb_buf_master {cmd args} {
    switch $cmd {
        getType { return CORE }
        isBlock { return 0 }
        default { return "" }
    }
}
"""


@pytest.mark.parametrize("stages", [False, True], ids=["one session", "staged"])
def test_a_top_with_cells_and_no_rows_stops_at_the_end_of_the_floorplan(
    tmp_path, monkeypatch, stages
):
    """A standard cell with no row to go to stops the run in the floorplan, naming it and the fixes, before global placement fails on the missing rows."""
    backend, artefacts = _backend(
        tmp_path, monkeypatch, env={"RB_STUB_NO_ROWS": "1"}, stages=stages
    )
    (tmp_path / "harness.tcl").write_text(
        _HARNESS.replace(
            "source [lindex $argv 0]", _UNPLACED_CELL + "source [lindex $argv 0]"
        )
    )

    res = backend.run()

    assert isinstance(res, PnrFailResults)
    assert (
        "floorplan: no rows left for 1 standard cell(s) (u_buf): leave a strip of "
        "the core without blocks for them, or turn off buffer-ports if they are "
        "port buffers"
    ) in res.results["desc"]
    (run_dir,) = _runs(artefacts)
    steps = [e["step"] for e in _events(run_dir) if e["event"] == "step_begin"]
    assert "place_pins" in steps
    assert "global_placement" not in steps
    assert "RB-NO-ROWS:" not in (artefacts / "pnr.log").read_text()
