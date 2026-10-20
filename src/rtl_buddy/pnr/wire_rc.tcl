
# A post-route `repair_design` at its own signal wire RC (`repair-design.wire-rc`,
# see `_repair_steps_tcl`). `rb::rc::save` keeps every scene's signal wire RC,
# `rb::rc::apply` replaces it and `rb::rc::restore` puts it back, so the
# `repair_timing` after it estimates the nets it changes at the RC the session
# had. Restoring does not invalidate parasitics: the nets `repair_design` changed
# keep their `wire-rc` estimate until they are estimated again. Clock wire RC is left
# alone. OpenROAD reads back only the mean of the horizontal and vertical signal
# RC, so `save` reads the horizontal one with the vertical one set to zero.
namespace eval rb::rc {
    variable saved {}
}

proc rb::rc::save {} {
    variable saved {}
    foreach scene [sta::scenes] {
        set res [est::wire_signal_resistance $scene]
        set cap [est::wire_signal_capacitance $scene]
        est::set_v_wire_signal_rc_cmd NULL $scene 0 0
        set h_res [expr {2 * [est::wire_signal_resistance $scene]}]
        set h_cap [expr {2 * [est::wire_signal_capacitance $scene]}]
        set v_res [expr {2 * $res - $h_res}]
        set v_cap [expr {2 * $cap - $h_cap}]
        est::set_v_wire_signal_rc_cmd NULL $scene $v_res $v_cap
        lappend saved [list $scene $h_res $h_cap $v_res $v_cap]
    }
}

proc rb::rc::restore {} {
    variable saved
    foreach entry $saved {
        lassign $entry scene h_res h_cap v_res v_cap
        est::set_h_wire_signal_rc_cmd NULL $scene $h_res $h_cap
        est::set_v_wire_signal_rc_cmd NULL $scene $v_res $v_cap
    }
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
