# Rotated-macro workaround for the `rb pnr` OpenROAD flow
# (`placement.rotated-macro-footprint`).
#
# Substituted into pnr.tcl just before `global_placement`, only when a macro
# can end up turned by 90 degrees: a `floorplan.macros` or macros-file
# orientation of R90, R270, MXR90 or MYR90, or `macro-placement: rtl-mp`.
# OpenROAD's negotiation legalizer blocks a rotated fixed macro's unrotated
# footprint, the master's width by height from the macro's lower-left corner
# (OpenROAD#11735), so it moves cells that global placement put in the part
# of that footprint outside the macro.
#
# `footprint` is plain arithmetic, so tests/test_pnr_floorplan_macros.py runs
# it under a bare Tcl interpreter.

namespace eval rb::rotated {}

proc rb::rotated::is_rotated {orient} {
    return [expr {$orient in {R90 R270 MXR90 MYR90}}]
}

# The macros in `macros` turned by 90 degrees.
proc rb::rotated::macros {macros} {
    set rotated {}
    foreach inst $macros {
        if {[is_rotated [$inst getOrient]]} {
            lappend rotated $inst
        }
    }
    return $rotated
}

# The box {x0 y0 x1 y1} of a `width` x `height` footprint from (x, y), grown
# by `halo_x` and `halo_y` and clipped to `core` {x0 y0 x1 y1}, or "" when
# nothing of it is inside the core.
proc rb::rotated::footprint {x y width height halo_x halo_y core} {
    lassign $core cx0 cy0 cx1 cy1
    set x0 [expr {max($x - $halo_x, $cx0)}]
    set y0 [expr {max($y - $halo_y, $cy0)}]
    set x1 [expr {min($x + $width + $halo_x, $cx1)}]
    set y1 [expr {min($y + $height + $halo_y, $cy1)}]
    if {$x0 >= $x1 || $y0 >= $y1} {
        return {}
    }
    return [list $x0 $y0 $x1 $y1]
}

# Create a hard placement blockage over each rotated macro's unrotated
# footprint, grown by its cell halo (MACRO_CELL_HALOS, else MACRO_CELL_HALO)
# and clipped to the core, and return the blockages.
proc rb::rotated::block_footprints {block rotated} {
    set core [$block getCoreArea]
    set core_box [list [$core xMin] [$core yMin] [$core xMax] [$core yMax]]
    set blockages {}
    foreach inst $rotated {
        set cell_halo [list $::MACRO_CELL_HALO $::MACRO_CELL_HALO]
        if {[dict exists $::MACRO_CELL_HALOS [$inst getName]]} {
            set cell_halo [dict get $::MACRO_CELL_HALOS [$inst getName]]
        }
        lassign $cell_halo halo_x halo_y
        set master [$inst getMaster]
        set bbox [$inst getBBox]
        set box [footprint [$bbox xMin] [$bbox yMin] \
            [$master getWidth] [$master getHeight] \
            [expr {max(0, int(round([ord::microns_to_dbu $halo_x])))}] \
            [expr {max(0, int(round([ord::microns_to_dbu $halo_y])))}] \
            $core_box]
        if {$box eq ""} {
            continue
        }
        lappend blockages [odb::dbBlockage_create $block {*}$box]
        puts ">>>   [$inst getName] [$inst getOrient]: blocked its unrotated footprint {$box} DBU for global placement"
    }
    return $blockages
}
