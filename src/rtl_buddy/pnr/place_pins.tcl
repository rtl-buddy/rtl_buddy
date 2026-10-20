# Skip test for `place_pins` in the `rb pnr` OpenROAD flow.
#
# Substituted into pnr.tcl just before `place_pins`, only when the run has an
# exact pin entry (`floorplan.pins` or `floorplan.pins-file` with a `location`).
# tests/test_pnr_floorplan_pins.py runs the proc under a bare Tcl interpreter
# with a stub database.

namespace eval rb::pins {}

# Whether every signal port already has a fixed pin (FIRM, LOCKED or COVER),
# which `place_pins` leaves where it is, so the flow can skip it. Supply ports
# are not counted. False for a block without signal ports.
proc rb::pins::all_placed {} {
    set count 0
    foreach bterm [[ord::get_db_block] getBTerms] {
        if {[$bterm getSigType] in {POWER GROUND}} {
            continue
        }
        incr count
        if {[$bterm getFirstPinPlacementStatus] ni {FIRM LOCKED COVER}} {
            return 0
        }
    }
    return [expr {$count > 0}]
}
