# Output port buffering for the rb pnr OpenROAD flow, for `buffer-ports` with
# `skip-clock-outputs` or `hold-outputs-at-pins`.
#
# Substituted into pnr.tcl after pin placement, where it runs
# `buffer_ports -outputs`. `nearest_site` takes and returns plain numbers and
# lists, so tests/test_pnr_port_buffers.py runs it under a bare Tcl
# interpreter. Geometry is in integral database units.

namespace eval rb::port_buffers {}

# The row site {x y orient} nearest `x y` whose row holds a cell `width` wide,
# from `rows`, each {xMin yMin xMax yMax site_pitch orient}, or an empty list
# when no row is wide enough. Distance is Manhattan, lower-left to lower-left.
proc rb::port_buffers::nearest_site {rows x y width} {
    set best {}
    set best_distance ""
    foreach row $rows {
        lassign $row x0 y0 x1 y1 pitch orient
        if {$x1 - $x0 < $width} {
            continue
        }
        set sx [expr {min(max($x, $x0), $x1 - $width)}]
        set sx [expr {$x0 + (($sx - $x0) / $pitch) * $pitch}]
        set distance [expr {abs($sx - $x) + abs($y0 - $y)}]
        if {$best_distance eq "" || $distance < $best_distance} {
            set best [list $sx $y0 $orient]
            set best_distance $distance
        }
    }
    return $best
}

# Moves each output port buffer `buffer_ports` added, an instance not in the
# dict `before`, to the row site nearest its pin and makes it FIRM, so global
# placement leaves it there. The place stage frees it after global placement.
proc rb::port_buffers::hold {block before} {
    set rows {}
    foreach row [$block getRows] {
        set box [$row getBBox]
        lappend rows [list [$box xMin] [$box yMin] [$box xMax] [$box yMax] \
            [$row getSpacing] [$row getOrient]]
    }
    set held 0
    foreach bterm [$block getBTerms] {
        set net [$bterm getNet]
        if {[$bterm getIoType] ne "OUTPUT" || $net eq "NULL"} {
            continue
        }
        foreach iterm [$net getITerms] {
            set inst [$iterm getInst]
            if {![$iterm isOutputSignal] || [dict exists $before [$inst getName]]} {
                continue
            }
            set master [$inst getMaster]
            set pin [$bterm getBBox]
            set x [expr {([$pin xMin] + [$pin xMax] - [$master getWidth]) / 2}]
            set y [expr {([$pin yMin] + [$pin yMax] - [$master getHeight]) / 2}]
            set site [nearest_site $rows $x $y [$master getWidth]]
            if {[llength $site] != 3} {
                puts ">>>   [$bterm getName]: no row holds [$master getName]; buffer left to global placement"
                continue
            }
            lassign $site sx sy orient
            $inst setOrient $orient
            $inst setLocation $sx $sy
            $inst setPlacementStatus FIRM
            incr held
        }
    }
    puts ">>>   $held output port buffer(s) fixed at their pins until global placement ends"
}

# `buffer_ports -outputs` with `args`. With `skip_clocks`, an output port that
# carries a clock (on the clock network, or a generated clock's source) gets no
# buffer: `buffer_ports` leaves a dont-touch net alone, so each such port's net
# is dont-touch for the command, and a net that was dont-touch stays so. With
# `hold`, the new buffers go to their pins (see `hold`).
proc rb::port_buffers::outputs {skip_clocks hold args} {
    set block [ord::get_db_block]
    set kept {}
    if {$skip_clocks} {
        foreach port [all_outputs] {
            set pin [sta::get_port_pin $port]
            if {![sta::is_clock $pin] && ![sta::is_clock_src $pin]} {
                continue
            }
            puts ">>>   [get_full_name $port] carries a clock: no port buffer"
            set net [[sta::sta_to_db_port $port] getNet]
            if {$net ne "NULL" && ![$net isDoNotTouch]} {
                $net setDoNotTouch 1
                lappend kept $net
            }
        }
    }
    set before [dict create]
    if {$hold} {
        foreach inst [$block getInsts] {
            dict set before [$inst getName] 1
        }
    }
    buffer_ports -outputs {*}$args
    foreach net $kept {
        $net setDoNotTouch 0
    }
    if {$hold} {
        hold $block $before
    }
}
