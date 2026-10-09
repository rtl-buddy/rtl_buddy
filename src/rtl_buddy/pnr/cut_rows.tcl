# Row cutting under hard placement blockages for the `rb pnr` OpenROAD flow.
#
# Substituted into pnr.tcl after the hard `floorplan.blockages`, ahead of tap
# insertion, only when the floorplan has hard blockages (rtl_buddy#773).
# `tapcell` fills every row, blockage or not, so a tap under a hard blockage
# fails `check_placement` (DPL-0033). Cut rows leave nothing there to fill,
# and `tapcell` puts its endcaps at the new row ends.
#
# OpenROAD's `cut_rows` cuts only around macros, so this works on the ODB rows
# directly. `segments` is plain arithmetic, so tests/test_pnr_cut_rows.py runs
# it under a bare Tcl interpreter; `cut_under` is the ODB half.

namespace eval rb::rows {}

# The sites a row keeps, as a list of `{first_site site_count}`.
#
# `x0` is the row's left edge, `pitch` its site pitch and `intervals` the
# `{x_min x_max}` spans of the blockages over the row, all in DBU. Each span
# rounds outward to whole sites, so every remnant stays on the row's site
# grid. A remnant of fewer than `min_sites` sites is dropped.
proc rb::rows::segments {x0 num_sites pitch intervals min_sites} {
    set cuts {}
    foreach span $intervals {
        lassign $span lo hi
        set first [expr {int(floor(double($lo - $x0) / $pitch))}]
        set last [expr {int(ceil(double($hi - $x0) / $pitch))}]
        lappend cuts [list [expr {max($first, 0)}] [expr {min($last, $num_sites)}]]
    }
    set kept {}
    set start 0
    foreach cut [lsort -integer -index 0 $cuts] {
        lassign $cut first last
        if {$first - $start >= $min_sites} {
            lappend kept [list $start [expr {$first - $start}]]
        }
        set start [expr {max($start, $last)}]
    }
    if {$num_sites - $start >= $min_sites} {
        lappend kept [list $start [expr {$num_sites - $start}]]
    }
    return $kept
}

# Cut every row of `block` that a box in `boxes` (`{x_min y_min x_max y_max}`
# in DBU) overlaps, and return how many rows it cut. A box that only touches
# a row's edge does not cut it. A remnant narrower than twice the row height
# is dropped: after its endcaps and a tap it holds hardly a cell.
proc rb::rows::cut_under {block boxes} {
    set cut 0
    foreach row [$block getRows] {
        set bbox [$row getBBox]
        set intervals {}
        foreach box $boxes {
            lassign $box x_min y_min x_max y_max
            if {$y_min < [$bbox yMax] && $y_max > [$bbox yMin]
                    && $x_min < [$bbox xMax] && $x_max > [$bbox xMin]} {
                lappend intervals [list $x_min $x_max]
            }
        }
        if {[llength $intervals] == 0} {
            continue
        }
        set name [$row getName]
        set site [$row getSite]
        set orient [$row getOrient]
        set direction [$row getDirection]
        set pitch [$row getSpacing]
        lassign [$row getOrigin] x0 y0
        set min_width [expr {2 * [$site getHeight]}]
        set min_sites [expr {max(1, int(ceil(double($min_width) / $pitch)))}]
        set kept [segments $x0 [$row getSiteCount] $pitch $intervals $min_sites]
        odb::dbRow_destroy $row
        set index 0
        foreach segment $kept {
            lassign $segment first count
            odb::dbRow_create $block ${name}_[incr index] $site \
                [expr {$x0 + $first * $pitch}] $y0 $orient $direction $count $pitch
        }
        incr cut
    }
    return $cut
}
