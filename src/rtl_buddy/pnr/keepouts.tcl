# Keep-out check for the `rb pnr` OpenROAD flow.
#
# Substituted into pnr.tcl after the hard `floorplan.blockages` and
# `floorplan.core-cutouts`, only when the floorplan has either. After macro
# placement (RTL-MP or a `macro-placement` hook) no macro may overlap one of
# them: RTL-MP weighs a blockage as a cost, so it can still place a macro on
# one. `overlaps` is plain arithmetic, so tests/test_pnr_keepouts.py runs it
# under a bare Tcl interpreter.

namespace eval rb::keepouts {}

# Whether boxes {x0 y0 x1 y1} overlap; touching edges do not.
proc rb::keepouts::overlaps {a b} {
    lassign $a ax0 ay0 ax1 ay1
    lassign $b bx0 by0 bx1 by1
    return [expr {$ax0 < $bx1 && $bx0 < $ax1 && $ay0 < $by1 && $by0 < $ay1}]
}

# Fail the run when a macro in `macros` overlaps a box in `boxes`
# ({x0 y0 x1 y1} in DBU, labelled by the same position in `labels`).
proc rb::keepouts::check {macros boxes labels dbu_per_micron} {
    foreach inst $macros {
        set bbox [$inst getBBox]
        set macro_box [list [$bbox xMin] [$bbox yMin] [$bbox xMax] [$bbox yMax]]
        foreach box $boxes label $labels {
            if {![overlaps $macro_box $box]} {
                continue
            }
            set um {}
            foreach v [concat $macro_box $box] {
                lappend um [format %g [expr {double($v) / $dbu_per_micron}]]
            }
            lassign $um mx0 my0 mx1 my1 kx0 ky0 kx1 ky1
            error [format "macro %s at (%s, %s)-(%s, %s) um overlaps %s (%s, %s)-(%s, %s) um after macro placement; fix the macro with a floorplan.macros location, widen placement.macro-channel, or move the blockage" \
                [$inst getName] $mx0 $my0 $mx1 $my1 $label $kx0 $ky0 $kx1 $ky1]
        }
    }
}
