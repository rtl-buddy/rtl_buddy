# IO pin constraints for the `rb pnr` OpenROAD flow (`floorplan.pins` and
# `floorplan.pins-file`).
#
# Substituted into pnr.tcl just before `place_pins`, only when the run has pin
# entries or sets `floorplan.pins-complete`. Each entry becomes one
# `rb::pins::constrain` or `rb::pins::place` call. Both fail the run, naming
# the entry, when a name or glob matches no port: `set_io_pin_constraint` would
# only warn and drop it.

namespace eval rb::pins {
    # Exact pins placed so far, port name -> entries.
    variable exact_entries {}
    # Set by the flow under `floorplan.pins-complete`: count the side and group
    # rules that cover each port, port name -> rules.
    variable track 0
    variable region_rules {}
}

# The ports `pattern` matches, or an error naming `where` and the pattern.
proc rb::pins::ports {where pattern} {
    set matched [get_ports -quiet $pattern]
    if {[llength $matched] == 0} {
        error "$where: no port matches '$pattern' (floorplan.pins)"
    }
    return $matched
}

# Constrain the ports `patterns` match to `region` (`edge:begin-end`, or ""
# for anywhere) and, with `group`, keep them together, in order with `order`.
# A port with an exact pin is left out, as is, under `track`, a port an
# earlier rule already covers: OpenROAD refuses a port in two regions
# (ODB-0239), and `check_complete` reports it instead.
proc rb::pins::constrain {where patterns region group order} {
    variable track
    variable exact_entries
    variable region_rules
    set matched {}
    foreach pattern $patterns {
        foreach port [ports $where $pattern] {
            dict set matched [[sta::sta_to_db_port $port] getName] 1
        }
    }
    set names {}
    foreach name [dict keys $matched] {
        if {[dict exists $exact_entries $name]} {
            continue
        }
        if {$track} {
            dict incr region_rules $name
            if {[dict get $region_rules $name] > 1} {
                continue
            }
        }
        lappend names $name
    }
    set left_out [expr {[dict size $matched] - [llength $names]}]
    if {[llength $names] == 0} {
        puts ">>>   $where: every matched port has an exact pin or an earlier rule; nothing to constrain"
        return
    }
    set command [list set_io_pin_constraint -pin_names \
        [expr {$left_out > 0 ? $names : $patterns}]]
    if {$region ne ""} {
        lappend command -region $region
    }
    if {$group} {
        lappend command -group
        if {$order} {
            lappend command -order
        }
    }
    if {$left_out > 0} {
        puts ">>>   $where: [lrange $command 0 1] [list $patterns] [lrange $command 3 end] ($left_out port(s) with an exact pin or an earlier rule left out)"
    } else {
        puts ">>>   $where: $command"
    }
    {*}$command
}

# The die edge nearest the point (x, y), in DBU.
proc rb::pins::nearest_edge {die x y} {
    lassign $die x_min y_min x_max y_max
    set best left
    set distance [expr {abs($x - $x_min)}]
    foreach {edge d} [list \
            right [expr {abs($x_max - $x)}] \
            bottom [expr {abs($y - $y_min)}] \
            top [expr {abs($y_max - $y)}]] {
        if {$d < $distance} {
            set best $edge
            set distance $d
        }
    }
    return $best
}

# Place the one port `pattern` matches with its centre at (x, y) microns on
# `layer`, or on the PDK pin layer of the nearest die edge when `layer` is "".
# `size` is {width height} in microns, or "" for the layer's minimum width and
# area. With `snap`, the pin moves to the die edge and the nearest routing
# track; without, its centre stays at (x, y) to the DBU. The pin is FIRM, so
# `place_pins` leaves it there. The placed box is reported on a
# `RB-FLOORPLAN-PIN: x0 y0 x1 y1 layer name` line in DBU, which rb compares
# with the routed DEF.
proc rb::pins::place {where pattern x y layer size {snap 1}} {
    set matched [ports $where $pattern]
    if {[llength $matched] != 1} {
        error "$where: '$pattern' matches [llength $matched] ports; a location places exactly one (floorplan.pins)"
    }
    set bterm [sta::sta_to_db_port [lindex $matched 0]]
    set name [$bterm getName]
    variable exact_entries
    dict incr exact_entries $name
    set block [ord::get_db_block]
    if {$layer eq ""} {
        set die [$block getDieArea]
        set edge [nearest_edge \
            [list [$die xMin] [$die yMin] [$die xMax] [$die yMax]] \
            [ord::microns_to_dbu $x] [ord::microns_to_dbu $y]]
        set layer [expr {$edge in {left right} ? $::PIN_LAYER_H : $::PIN_LAYER_V}]
    }
    set command [list place_pin -pin_name $name -layer $layer -location [list $x $y]]
    if {$snap} {
        lappend command -force_to_die_boundary
    }
    if {$size ne ""} {
        lappend command -pin_size $size
    }
    {*}$command
    set box [lindex [[lindex [$bterm getBPins] 0] getBoxes] 0]
    set dbu [expr {double([$block getDbUnitsPerMicron])}]
    puts [format ">>>   %s: placed pin %s on %s centred at (%.3f, %.3f) um (requested (%s, %s))" \
        $where $name $layer \
        [expr {([$box xMin] + [$box xMax]) / 2 / $dbu}] \
        [expr {([$box yMin] + [$box yMax]) / 2 / $dbu}] $x $y]
    puts "RB-FLOORPLAN-PIN: [$box xMin] [$box yMin] [$box xMax] [$box yMax] [[$box getTechLayer] getName] $name"
}

# Classify the signal ports by the entries recorded since `track` was set: a
# port with more than one exact entry, a port with no exact entry that several
# side or group rules match, and a port nothing matches. Supply ports are skipped.
# Each non-empty class is reported on a `RB-PINS-COMPLETE: class count ports`
# line naming up to `limit` ports, then the flow stops with an error.
proc rb::pins::check_complete {{limit 10}} {
    variable exact_entries
    variable region_rules
    set classes [dict create duplicate-exact {} several-regions {} uncovered {}]
    foreach bterm [[ord::get_db_block] getBTerms] {
        if {[$bterm getSigType] in {POWER GROUND}} {
            continue
        }
        set name [$bterm getName]
        set exact_count [expr {[dict exists $exact_entries $name] ? [dict get $exact_entries $name] : 0}]
        if {$exact_count > 1} {
            dict lappend classes duplicate-exact $name
        } elseif {$exact_count == 0} {
            set region_count [expr {[dict exists $region_rules $name] ? [dict get $region_rules $name] : 0}]
            if {$region_count > 1} {
                dict lappend classes several-regions $name
            } elseif {$region_count == 0} {
                dict lappend classes uncovered $name
            }
        }
    }
    set problems {}
    dict for {class names} $classes {
        if {[llength $names] == 0} {
            continue
        }
        set shown [lrange [lsort $names] 0 [expr {$limit - 1}]]
        puts "RB-PINS-COMPLETE: $class [llength $names] [join $shown { }]"
        lappend problems "[llength $names] $class"
    }
    if {[llength $problems] > 0} {
        error "floorplan.pins-complete: [join $problems {, }] port(s)"
    }
    puts ">>>   floorplan.pins-complete: every signal port is covered"
}
