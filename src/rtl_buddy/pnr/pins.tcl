# IO pin constraints for the `rb pnr` OpenROAD flow (`floorplan.pins`).
#
# Substituted into pnr.tcl just before `place_pins`, only when the run sets
# `floorplan.pins`. Each entry becomes one `rb::pins::constrain` or
# `rb::pins::place` call. Both fail the run, naming the entry, when a name or
# glob matches no port: `set_io_pin_constraint` would only warn and drop it.

namespace eval rb::pins {}

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
proc rb::pins::constrain {where patterns region group order} {
    foreach pattern $patterns {
        ports $where $pattern
    }
    set command [list set_io_pin_constraint -pin_names $patterns]
    if {$region ne ""} {
        lappend command -region $region
    }
    if {$group} {
        lappend command -group
        if {$order} {
            lappend command -order
        }
    }
    puts ">>>   $where: $command"
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
# area. The pin moves to the die edge and the nearest routing track, and is
# FIRM, so `place_pins` leaves it there.
proc rb::pins::place {where pattern x y layer size} {
    set matched [ports $where $pattern]
    if {[llength $matched] != 1} {
        error "$where: '$pattern' matches [llength $matched] ports; a location places exactly one (floorplan.pins)"
    }
    set bterm [sta::sta_to_db_port [lindex $matched 0]]
    set name [$bterm getName]
    set block [ord::get_db_block]
    if {$layer eq ""} {
        set die [$block getDieArea]
        set edge [nearest_edge \
            [list [$die xMin] [$die yMin] [$die xMax] [$die yMax]] \
            [ord::microns_to_dbu $x] [ord::microns_to_dbu $y]]
        set layer [expr {$edge in {left right} ? $::PIN_LAYER_H : $::PIN_LAYER_V}]
    }
    set command [list place_pin -pin_name $name -layer $layer \
        -location [list $x $y] -force_to_die_boundary]
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
}
