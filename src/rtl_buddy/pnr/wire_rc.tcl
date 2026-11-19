# The signal wire RC a post-route repair estimates the nets it changes at (see
# `_repair_steps_tcl`). With `repair-design.wire-rc`, `rb::rc::save` keeps every
# scene's signal wire RC, `rb::rc::apply` replaces it and `rb::rc::restore` puts
# it back after the last repair of the steps. `rb::rc::ideal` runs a hold repair
# with it at zero. No setter invalidates parasitics: a net a repair changed keeps
# the estimate it got until OpenROAD estimates it again. Clock wire RC is left
# alone. OpenROAD reads back only the mean of the horizontal and vertical signal
# RC, so `rb::rc::current` reads the horizontal one with the vertical one set to
# zero.
namespace eval rb::rc {
    variable saved {}
}

proc rb::rc::current {} {
    set each {}
    foreach scene [sta::scenes] {
        set res [est::wire_signal_resistance $scene]
        set cap [est::wire_signal_capacitance $scene]
        est::set_v_wire_signal_rc_cmd NULL $scene 0 0
        set h_res [expr {2 * [est::wire_signal_resistance $scene]}]
        set h_cap [expr {2 * [est::wire_signal_capacitance $scene]}]
        set v_res [expr {2 * $res - $h_res}]
        set v_cap [expr {2 * $cap - $h_cap}]
        est::set_v_wire_signal_rc_cmd NULL $scene $v_res $v_cap
        lappend each [list $scene $h_res $h_cap $v_res $v_cap]
    }
    return $each
}

proc rb::rc::put {each} {
    foreach entry $each {
        lassign $entry scene h_res h_cap v_res v_cap
        est::set_h_wire_signal_rc_cmd NULL $scene $h_res $h_cap
        est::set_v_wire_signal_rc_cmd NULL $scene $v_res $v_cap
    }
}

proc rb::rc::save {} {
    variable saved [current]
}

proc rb::rc::restore {} {
    variable saved
    put $saved
}

# Run `script`, a hold repair, with every scene's signal wire RC at zero, then
# put the RC back. A hold buffer the repair tries and rolls back leaves its net
# on an estimate in place of the extracted parasitics; with no wire that estimate
# is never slower than the route, so an endpoint behind it cannot read as met
# while it fails. An OpenROAD without scenes or the `est::` setters runs `script`
# at the RC the session has.
proc rb::rc::ideal {script} {
    if {![llength [info commands ::sta::scenes]]
        || ![llength [info commands ::est::set_h_wire_signal_rc_cmd]]} {
        puts "rb: no sta::scenes or est:: wire RC commands; the hold repair\
            estimates at the session's signal wire RC"
        return [uplevel 1 $script]
    }
    set held [current]
    set none {}
    foreach entry $held {
        lappend none [list [lindex $entry 0] 0 0 0 0]
    }
    put $none
    set code [catch {uplevel 1 $script} result options]
    put $held
    if {$code == 0} { return $result }
    return -options [dict incr options -level] $result
}

# Set every scene's signal wire RC to `spec`: {resistance R capacitance C} in
# kohm/um and fF/um, or {layer NAME} for that layer's RC in the scene
# (`set_layer_rc`), else its LEF RC, as `set_wire_rc -signal -layer` takes it.
proc rb::rc::apply {spec} {
    if {[dict exists $spec layer]} {
        set name [dict get $spec layer]
        set layer [[ord::get_db_tech] findLayer $name]
        if {$layer eq "NULL"} {
            error "post-route-repair.repair-design.wire-rc: no layer $name"
        }
    }
    foreach scene [sta::scenes] {
        if {![info exists layer]} {
            set res [expr {[dict get $spec resistance] * 1e9}]
            set cap [expr {[dict get $spec capacitance] * 1e-9}]
        } else {
            set res [est::layer_resistance $layer $scene]
            set cap [est::layer_capacitance $layer $scene]
            if {$res == 0 && $cap == 0} {
                lassign [est::dblayer_wire_rc $layer] res cap
            }
        }
        est::set_h_wire_signal_rc_cmd NULL $scene $res $cap
        est::set_v_wire_signal_rc_cmd NULL $scene $res $cap
        if {abs([est::wire_signal_resistance $scene] - $res) > 1e-6 * $res
            || abs([est::wire_signal_capacitance $scene] - $cap) > 1e-6 * $cap} {
            error "post-route-repair.repair-design.wire-rc did not take effect:\
                the session sets signal wire RC per technology"
        }
    }
}

# Every scene's mean signal wire RC in kohm/um and fF/um, given once when the
# scenes agree.
proc rb::rc::describe {} {
    set each {}
    foreach scene [sta::scenes] {
        lappend each [sta::get_name $scene] [format "%.4g kohm/um, %.4g fF/um" \
            [expr {[est::wire_signal_resistance $scene] * 1e-9}] \
            [expr {[est::wire_signal_capacitance $scene] * 1e9}]]
    }
    set values [lsort -unique [dict values $each]]
    if {[llength $values] == 1} {
        return [lindex $values 0]
    }
    set parts {}
    dict for {name value} $each {
        lappend parts "$name $value"
    }
    return [join $parts "; "]
}
