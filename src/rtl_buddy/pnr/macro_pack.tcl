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

# Which axes an anchor corner mirrors (#105). The packer always packs from
# the lower-left corner of its own frame; any other anchor is that same
# packing reflected across the core's vertical and/or horizontal centre
# line, so the halo, the tallest-first order and overlap freedom carry over
# unchanged — a reflection preserves every distance.
proc rb::macro_pack::mirror_axes {anchor} {
    switch -- $anchor {
        lower-left { return {0 0} }
        lower-right { return {1 0} }
        upper-left { return {0 1} }
        upper-right { return {1 1} }
    }
    error "unknown macro anchor \"$anchor\": expected lower-left, lower-right, upper-left or upper-right (floorplan.macro-anchor)"
}

# Snap a macro's low edge along one axis, in the packing frame. `value` is
# the edge before snapping, `size` the macro's extent along the axis, and
# `lo`/`hi` the core's real extent; `mirrored` says whether the packing frame
# is this axis reflected (`lo + hi - x`).
#
# The site grid belongs to the real core — it counts from its lower-left
# corner whatever the anchor — so a mirrored macro is snapped by its real
# origin, `lo + hi - value - size`, and *down*: away from the anchor edge,
# which in the packing frame is up. Either way snapping can only widen a
# channel, never eat into a halo (#639).
proc rb::macro_pack::snap_edge {value size grid lo hi mirrored} {
    if {!$mirrored} {
        return [snap_up $value $grid $lo]
    }
    if {$grid <= 1} {
        return $value
    }
    set origin [expr {$lo + $hi - $value - $size}]
    set snapped [expr {$lo + int(floor(double($origin - $lo) / $grid)) * $grid}]
    return [expr {$lo + $hi - $snapped - $size}]
}

# The first keep-out the box {x0 y0 x1 y1} overlaps (touching is not
# overlapping), or an empty string.
proc rb::macro_pack::blocker {keepouts x0 y0 x1 y1} {
    foreach keepout $keepouts {
        lassign $keepout kx0 ky0 kx1 ky1
        if {$x0 < $kx1 && $kx0 < $x1 && $y0 < $ky1 && $ky0 < $y1} {
            return $keepout
        }
    }
    return ""
}

# Pack `macros` — a list of {name width height} — into `core`, a list of
# {x_min y_min x_max y_max}. `halo` is the minimum channel kept between two
# macros and between a macro and every core edge; `grid` is the placement
# grid every origin is snapped to, as {site_width row_height}, counted from
# the core's lower-left corner.
#
# `anchor` is the core corner the packing starts from (#105): the rows fill
# away from it, so the opposite edges stay free for IO pins. `keepouts` is a
# list of {x0 y0 x1 y1} rectangles — the hard placement blockages — that no
# macro may overlap; a macro that would is pushed past the keep-out along
# its row, and a row a keep-out leaves no room in is skipped. A keep-out
# needs no halo: it holds no standard cells for the channel to serve.
#
# Returns a dict of name -> {x y}, or an empty result when the macros do not
# fit. Callers pass a non-empty macro list.
#
# (The space before each argument list's closing brace keeps two closing
# braces in a row — the flow template's placeholder delimiter — out of the
# rendered pnr.tcl.)
proc rb::macro_pack::place {core macros halo grid {anchor lower-left} {keepouts ""} } {
    lassign $core x_min y_min x_max y_max
    lassign $grid grid_x grid_y
    lassign [mirror_axes $anchor] flip_x flip_y

    # Everything below works in the packing frame, where the anchor is the
    # lower-left corner; keep-outs are reflected into it once, here.
    set blocks {}
    foreach keepout $keepouts {
        lassign $keepout kx0 ky0 kx1 ky1
        if {$flip_x} {
            lassign [list [expr {$x_min + $x_max - $kx1}] [expr {$x_min + $x_max - $kx0}]] kx0 kx1
        }
        if {$flip_y} {
            lassign [list [expr {$y_min + $y_max - $ky1}] [expr {$y_min + $y_max - $ky0}]] ky0 ky1
        }
        lappend blocks [list $kx0 $ky0 $kx1 $ky1]
    }

    set row_start [expr {$x_min + $halo}]
    set right [expr {$x_max - $halo}]
    set top [expr {$y_max - $halo}]

    set placement [dict create]
    # `shelf_y` is where the current row starts before snapping; each macro
    # snaps its own bottom from it (one value for all of them unless the
    # frame is mirrored in y, where the snap depends on the macro's height).
    # `shelf_top` is the highest macro top in the row, which the next row
    # clears by the halo.
    set shelf_y [expr {$y_min + $halo}]
    set shelf_top $shelf_y
    set shelf_used 0
    set cursor $row_start
    foreach macro [sort $macros] {
        lassign $macro name width height
        while 1 {
            set x [snap_edge $cursor $width $grid_x $x_min $x_max $flip_x]
            set y [snap_edge $shelf_y $height $grid_y $y_min $y_max $flip_y]
            if {$x + $width > $right} {
                # A macro that overruns a row that holds something starts
                # the next one.
                if {$shelf_used} {
                    set shelf_y [expr {$shelf_top + $halo}]
                    set shelf_top $shelf_y
                    set shelf_used 0
                    set cursor $row_start
                    continue
                }
                # An empty row that cannot take the macro is either blocked
                # by keep-outs — then move up past the lowest one in the
                # macro's way — or the macro is too wide for the core, which
                # no number of rows fixes.
                set next ""
                foreach block $blocks {
                    lassign $block kx0 ky0 kx1 ky1
                    if {$ky0 < $y + $height && $ky1 > $y && ($next eq "" || $ky1 < $next)} {
                        set next $ky1
                    }
                }
                if {$next eq ""} {
                    return [dict create]
                }
                set shelf_y $next
                set shelf_top $shelf_y
                set cursor $row_start
                continue
            }
            if {$y + $height > $top} {
                return [dict create]
            }
            set hit [blocker $blocks $x $y [expr {$x + $width}] [expr {$y + $height}]]
            if {$hit ne ""} {
                set cursor [lindex $hit 2]
                continue
            }
            break
        }
        set real_x [expr {$flip_x ? $x_min + $x_max - $x - $width : $x}]
        set real_y [expr {$flip_y ? $y_min + $y_max - $y - $height : $y}]
        dict set placement $name [list $real_x $real_y]
        set cursor [expr {$x + $width + $halo}]
        set shelf_used 1
        if {$y + $height > $shelf_top} {
            set shelf_top [expr {$y + $height}]
        }
    }
    return $placement
}

# The core is scaled about its anchor corner, so keep-outs keep their
# distance from the corner the packing starts in.
proc rb::macro_pack::fits {core macros halo grid scale {anchor lower-left} {keepouts ""} } {
    lassign $core x_min y_min x_max y_max
    lassign [mirror_axes $anchor] flip_x flip_y
    set width [expr {int(ceil(($x_max - $x_min) * $scale))}]
    set height [expr {int(ceil(($y_max - $y_min) * $scale))}]
    set x0 [expr {$flip_x ? $x_max - $width : $x_min}]
    set y0 [expr {$flip_y ? $y_max - $height : $y_min}]
    set scaled [list $x0 $y0 [expr {$x0 + $width}] [expr {$y0 + $height}]]
    return [expr {[dict size [place $scaled $macros $halo $grid $anchor $keepouts]] > 0}]
}

# The smallest core with this core's aspect ratio that `place` fits, found by
# bisecting a scale factor. The number is advisory — it is what this packer
# needs, not a proof about every possible packing — and is what the no-fit
# diagnostic quotes. Returns {width height} in DBU, or an empty list when
# even 64x the current core does not hold the macros.
proc rb::macro_pack::min_core {core macros halo grid {anchor lower-left} {keepouts ""} } {
    lassign $core x_min y_min x_max y_max
    set hi 1.0
    while {$hi <= 64.0 && ![fits $core $macros $halo $grid $hi $anchor $keepouts]} {
        set hi [expr {$hi * 2.0}]
    }
    if {$hi > 64.0} {
        return {}
    }
    set lo [expr {$hi == 1.0 ? 0.0 : $hi / 2.0}]
    for {set i 0} {$i < 40} {incr i} {
        set mid [expr {($lo + $hi) / 2.0}]
        if {[fits $core $macros $halo $grid $mid $anchor $keepouts]} {
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

proc rb::macro_pack::no_fit_message {core macros halo grid dbu_per_micron {anchor lower-left} {keepouts ""} } {
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
    if {$anchor ne "lower-left"} {
        lappend lines "  packing from the $anchor core corner (floorplan.macro-anchor)"
    }
    if {[llength $keepouts] > 0} {
        lappend lines [format "  keeping every macro out of %d hard placement blockage(s) (floorplan.blockages)" \
            [llength $keepouts]]
    }
    lappend lines [format "  macros: largest footprint %s x %s um, %.1f um2 of macro area in a %.1f um2 core" \
        [microns $widest $dbu_per_micron] [microns $tallest $dbu_per_micron] \
        [expr {$macro_area / $um2}] [expr {double($core_w) * $core_h / $um2}]]
    set minimum [min_core $core $macros $halo $grid $anchor $keepouts]
    if {[llength $minimum] == 2} {
        lassign $minimum min_w min_h
        lappend lines [format "  smallest core at this aspect ratio that would fit: %s x %s um" \
            [microns $min_w $dbu_per_micron] [microns $min_h $dbu_per_micron]]
    } else {
        lappend lines "  no core up to 64x this one fits these macros at this halo"
    }
    if {[llength $keepouts] > 0} {
        lappend lines "  lower the floorplan utilization, change its aspect ratio, lower placement.macro-halo, or shrink or move a hard floorplan.blockages entry"
    } else {
        lappend lines "  lower the floorplan utilization, change its aspect ratio, or lower placement.macro-halo"
    }
    return [join $lines "\n"]
}

# Entry point used by the flow: pack or fail with the diagnostic. The flow
# passes `anchor` and `keepouts` only when a run sets them, so a run that
# sets neither calls it exactly as before (#105).
proc rb::macro_pack::solve {core macros halo grid dbu_per_micron {anchor lower-left} {keepouts ""} } {
    set placement [place $core $macros $halo $grid $anchor $keepouts]
    if {[dict size $placement] == 0} {
        error [no_fit_message $core $macros $halo $grid $dbu_per_micron $anchor $keepouts]
    }
    return $placement
}
