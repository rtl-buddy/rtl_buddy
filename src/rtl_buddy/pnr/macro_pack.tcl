# Size-aware macro packing for the `rb pnr` OpenROAD flow.
#
# This file is substituted into pnr.tcl verbatim, ahead of the macro
# placement stage. It deliberately contains no OpenROAD commands: every
# procedure takes and returns plain numbers, so the packing itself is unit
# tested under a bare Tcl interpreter (tests/test_pnr_macro_pack.py) rather
# than only through a full P&R run.
#
# All geometry is in database units and integral; `dbu_per_micron` is used
# only to phrase the diagnostic. The packer is a first-fit shelf (row)
# packer: macros are placed tallest first into rows whose height is the
# tallest macro in the row, which is the classic size-aware alternative to
# one grid slot per macro sized for the largest of them (#626).

namespace eval rb::macro_pack {}

# Round up to the next multiple of `grid` counted from `origin`. Macro
# origins are snapped up, not down, so snapping can only widen a channel and
# never eat into the halo of the neighbour below or to the left.
#
# The grid is the standard-cell site grid — the site width in x, the row
# height in y — measured from the core's lower-left corner, where
# `initialize_floorplan` starts its rows. A macro whose bottom sits between
# two rows is legal for the overlap check, which snaps it down to the row it
# straddles, but the detailed placer's padding check rounds it to the
# *nearest* row and, when that is the row above, scans the row past the
# macro's top edge — legally full of standard cells — as the macro's own and
# reports a padding violation (DPL-0011) that no legalization can clear
# (#639). Row-aligned origins make both views agree.
proc rb::macro_pack::snap_up {value grid origin} {
    if {$grid <= 1} {
        return $value
    }
    set offset [expr {$value - $origin}]
    return [expr {$origin + int(ceil(double($offset) / $grid)) * $grid}]
}

# Tallest first, then widest, then by name. A total order, so the placement
# never depends on the order the database hands back instances.
proc rb::macro_pack::compare {a b} {
    lassign $a name_a width_a height_a
    lassign $b name_b width_b height_b
    if {$height_a != $height_b} {
        return [expr {$height_b < $height_a ? -1 : 1}]
    }
    if {$width_a != $width_b} {
        return [expr {$width_b < $width_a ? -1 : 1}]
    }
    return [string compare $name_a $name_b]
}

proc rb::macro_pack::sort {macros} {
    return [lsort -command rb::macro_pack::compare $macros]
}

# Pack `macros` — a list of {name width height} — into `core`, a list of
# {x_min y_min x_max y_max}. `halo` is the minimum channel kept between two
# macros and between a macro and every core edge; `grid` is the placement
# grid every origin is snapped to, as {site_width row_height}, counted from
# the core's lower-left corner.
#
# Returns a dict of name -> {x y}, or an empty result when the macros do not
# fit. Callers pass a non-empty macro list.
proc rb::macro_pack::place {core macros halo grid} {
    lassign $core x_min y_min x_max y_max
    lassign $grid grid_x grid_y
    set left [snap_up [expr {$x_min + $halo}] $grid_x $x_min]
    set bottom [snap_up [expr {$y_min + $halo}] $grid_y $y_min]
    set right [expr {$x_max - $halo}]
    set top [expr {$y_max - $halo}]

    set placement [dict create]
    set shelf_y $bottom
    set shelf_height 0
    set cursor $left
    foreach macro [sort $macros] {
        lassign $macro name width height
        set x [snap_up $cursor $grid_x $x_min]
        # A macro that overruns the row starts the next one. The guard on
        # the row being non-empty keeps a macro too wide for the core from
        # opening an endless run of empty rows; it fails the fit check
        # below instead.
        if {$shelf_height > 0 && $x + $width > $right} {
            set shelf_y [snap_up [expr {$shelf_y + $shelf_height + $halo}] $grid_y $y_min]
            set shelf_height 0
            set x $left
        }
        if {$x + $width > $right || $shelf_y + $height > $top} {
            return [dict create]
        }
        dict set placement $name [list $x $shelf_y]
        set cursor [expr {$x + $width + $halo}]
        if {$height > $shelf_height} {
            set shelf_height $height
        }
    }
    return $placement
}

proc rb::macro_pack::fits {core macros halo grid scale} {
    lassign $core x_min y_min x_max y_max
    set width [expr {int(ceil(($x_max - $x_min) * $scale))}]
    set height [expr {int(ceil(($y_max - $y_min) * $scale))}]
    set scaled [list $x_min $y_min [expr {$x_min + $width}] [expr {$y_min + $height}]]
    return [expr {[dict size [place $scaled $macros $halo $grid]] > 0}]
}

# The smallest core with this core's aspect ratio that `place` fits, found by
# bisecting a scale factor. The number is advisory — it is what this packer
# needs, not a proof about every possible packing — and is what the no-fit
# diagnostic quotes. Returns {width height} in DBU, or an empty list when
# even 64x the current core does not hold the macros.
proc rb::macro_pack::min_core {core macros halo grid} {
    lassign $core x_min y_min x_max y_max
    set hi 1.0
    while {$hi <= 64.0 && ![fits $core $macros $halo $grid $hi]} {
        set hi [expr {$hi * 2.0}]
    }
    if {$hi > 64.0} {
        return {}
    }
    set lo [expr {$hi == 1.0 ? 0.0 : $hi / 2.0}]
    for {set i 0} {$i < 40} {incr i} {
        set mid [expr {($lo + $hi) / 2.0}]
        if {[fits $core $macros $halo $grid $mid]} {
            set hi $mid
        } else {
            set lo $mid
        }
    }
    return [list \
        [expr {int(ceil(($x_max - $x_min) * $hi))}] \
        [expr {int(ceil(($y_max - $y_min) * $hi))}]]
}

proc rb::macro_pack::microns {dbu dbu_per_micron} {
    return [format %.3f [expr {double($dbu) / $dbu_per_micron}]]
}

proc rb::macro_pack::no_fit_message {core macros halo grid dbu_per_micron} {
    lassign $core x_min y_min x_max y_max
    set core_w [expr {$x_max - $x_min}]
    set core_h [expr {$y_max - $y_min}]

    set widest 0
    set tallest 0
    set macro_area 0.0
    foreach macro $macros {
        lassign $macro name width height
        if {$width > $widest} {
            set widest $width
        }
        if {$height > $tallest} {
            set tallest $height
        }
        set macro_area [expr {$macro_area + double($width) * $height}]
    }
    set um2 [expr {double($dbu_per_micron) * $dbu_per_micron}]

    set lines {}
    lappend lines [format "%d macros do not fit the %s x %s um floorplan core" \
        [llength $macros] \
        [microns $core_w $dbu_per_micron] [microns $core_h $dbu_per_micron]]
    lappend lines [format "  tried: shelf packing, tallest macro first, keeping a %s um halo (placement.macro-halo) at every core edge and between macros" \
        [microns $halo $dbu_per_micron]]
    lappend lines [format "  macros: largest footprint %s x %s um, %.1f um2 of macro area in a %.1f um2 core" \
        [microns $widest $dbu_per_micron] [microns $tallest $dbu_per_micron] \
        [expr {$macro_area / $um2}] [expr {double($core_w) * $core_h / $um2}]]
    set minimum [min_core $core $macros $halo $grid]
    if {[llength $minimum] == 2} {
        lassign $minimum min_w min_h
        lappend lines [format "  smallest core at this aspect ratio that would fit: %s x %s um" \
            [microns $min_w $dbu_per_micron] [microns $min_h $dbu_per_micron]]
    } else {
        lappend lines "  no core up to 64x this one fits these macros at this halo"
    }
    lappend lines "  lower the floorplan utilization, change its aspect ratio, or lower placement.macro-halo"
    return [join $lines "\n"]
}

# Entry point used by the flow: pack or fail with the diagnostic.
proc rb::macro_pack::solve {core macros halo grid dbu_per_micron} {
    set placement [place $core $macros $halo $grid]
    if {[dict size $placement] == 0} {
        error [no_fit_message $core $macros $halo $grid $dbu_per_micron]
    }
    return $placement
}
