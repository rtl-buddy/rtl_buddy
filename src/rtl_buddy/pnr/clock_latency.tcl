# The `clock-latency` report of the rb pnr OpenROAD flow.
#
# Substituted into pnr.tcl after insts.tcl, only when the run sets the key, and
# run in the final reports, on the final parasitics. For each group of clock
# sinks it prints the late arrival of the clock's rising edge at the sinks,
# per corner. Every step is caught, so a failure loses only that group's rows.

namespace eval rb::clock_latency {}

# The register clock pins that a selector names: `input-capture`, the
# registers whose data pin captures an input port through combinational logic,
# or `output-launch`, those that launch an output port. An input's endpoints
# also include asynchronous set and reset pins and clock-gating checks, so
# only register data pins that are not asynchronous count. OpenSTA 3 names the
# traversals `get_fanout` and `get_fanin`, earlier releases `all_fanout` and
# `all_fanin`.
proc rb::clock_latency::selected {selector} {
    set fanout [expr {[info commands ::get_fanout] ne "" ? "get_fanout" : "all_fanout"}]
    set fanin [expr {[info commands ::get_fanin] ne "" ? "get_fanin" : "all_fanin"}]
    set pins {}
    if {$selector eq "input-capture"} {
        set inputs [all_inputs -no_clocks]
        if {[llength $inputs] == 0} {
            return {}
        }
        set data [dict create]
        foreach pin [all_registers -data_pins] {
            dict set data [get_full_name $pin] 1
        }
        foreach pin [all_registers -async_pins] {
            dict unset data [get_full_name $pin]
        }
        foreach end [$fanout -from $inputs -endpoints_only -flat] {
            if {[sta::object_type $end] ne "Pin"
                    || ![dict exists $data [get_full_name $end]]} {
                continue
            }
            foreach pin [get_pins -of_objects [get_cells -of_objects $end]] {
                if {[get_property $pin is_register_clock]} {
                    lappend pins $pin
                }
            }
        }
    } else {
        set outputs [all_outputs]
        if {[llength $outputs] == 0} {
            return {}
        }
        foreach start [$fanin -to $outputs -startpoints_only -flat] {
            if {[sta::object_type $start] eq "Pin"
                    && [get_property $start is_register_clock]} {
                lappend pins $start
            }
        }
    }
    return $pins
}

# The timer's pins of a group's `sinks`: selectors and `<instance>/<pin>`
# specs, once each.
proc rb::clock_latency::sinks {sinks} {
    set block [ord::get_db_block]
    set seen [dict create]
    set pins {}
    foreach spec $sinks {
        if {$spec in {input-capture output-launch}} {
            set found [selected $spec]
        } else {
            set found {}
            foreach iterm [::rb::insts::pins $block $spec] {
                set pin [get_pins -quiet \
                    "[[$iterm getInst] getName]/[[$iterm getMTerm] getName]"]
                if {$pin ne ""} {
                    lappend found $pin
                }
            }
        }
        foreach pin $found {
            set name [get_full_name $pin]
            if {![dict exists $seen $name]} {
                dict set seen $name 1
                lappend pins $pin
            }
        }
    }
    return $pins
}

# The `report_arrival` option that names corner `name`: `-scene` on OpenSTA 3,
# `-corner` before it.
proc rb::clock_latency::corner_args {name} {
    if {[info commands ::sta::find_scene] ne ""} {
        return [list -scene $name]
    }
    return [list -corner $name]
}

# The late arrival at `pin` of the rising edge of any clock that reaches it,
# the latest if several do, at the corner `report_arrival` takes from
# `corner_args`; "" for a pin off the clock network, such as a data pin.
proc rb::clock_latency::late_arrival {pin corner_args} {
    if {[llength [get_property $pin clocks]] == 0} {
        return ""
    }
    # The body runs in `with_output_to_variable`'s own scope.
    with_output_to_variable text \
        [list report_arrival {*}$corner_args -digits 6 $pin]
    set worst ""
    foreach line [split $text "\n"] {
        # `(clk ^) r early:late f early:late`, `---` for a transition it lacks.
        if {![regexp {^\s*\(\S+ \^\)\s+r\s+(\S+):(\S+)\s+f\s+(\S+):(\S+)} $line \
                -> r_early r_late f_early f_late]} {
            continue
        }
        foreach late [list $r_late $f_late] {
            if {[string is double -strict $late]
                    && ($worst eq "" || $late > $worst)} {
                set worst $late
            }
        }
    }
    return $worst
}

# Print `tag group corner sinks timed min max mean` for each group, each
# {name sinks}, at each of `corners`, each {name corner_args ?mode?}; `-` for a
# value without timed sinks. Times are in the timer's unit. A corner with a
# `mode` is measured with that SDC mode current, since a pin's clocks are read
# in the current mode, not the scene's; the mode current before is restored.
proc rb::clock_latency::report {tag groups corners} {
    set restore ""
    foreach corner $corners {
        if {[lindex $corner 2] ne ""} {
            set restore [sta::cmd_mode_name]
        }
    }
    foreach group $groups {
        lassign $group name specs
        if {[catch {set pins [sinks $specs]} err]} {
            puts "rb: clock latency of $name unavailable: $err"
            continue
        }
        foreach corner $corners {
            lassign $corner corner_name corner_args mode
            if {[catch {
                if {$mode ne ""} {
                    set_mode $mode
                }
                set arrivals {}
                foreach pin $pins {
                    set late [late_arrival $pin $corner_args]
                    if {$late ne ""} {
                        lappend arrivals $late
                    }
                }
                if {[llength $arrivals] == 0} {
                    set stats {- - -}
                } else {
                    set sum 0.0
                    foreach late $arrivals {
                        set sum [expr {$sum + $late}]
                    }
                    set stats [list \
                        [tcl::mathfunc::min {*}$arrivals] \
                        [tcl::mathfunc::max {*}$arrivals] \
                        [expr {$sum / [llength $arrivals]}]]
                }
                puts "$tag $name $corner_name [llength $pins] [llength $arrivals] [join $stats { }]"
            } err]} {
                puts "rb: clock latency of $name at $corner_name unavailable: $err"
            }
        }
        if {$restore ne ""} {
            set_mode $restore
        }
    }
}
