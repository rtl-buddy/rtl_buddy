# The `cts-root-taps` run key for the rb pnr OpenROAD flow.
#
# Substituted into pnr.tcl after insts.tcl, only when the run sets the key.
# TritonCTS moves every input pin on a clock net behind a buffer it inserts. A
# root tap is an input pin that must stay on the root net instead, such as the input of a buffer
# that forwards the clock to an output port. `detach` takes each tap off its
# net just before `clock_tree_synthesis`; `reattach` puts it back on the net of
# the same name after CTS and `repair_clock_nets`, before the `after-cts` hooks.
#
# A dont-touch instance (`dont-touch`, or a hook's `set_dont_touch`) cannot
# have a pin disconnected (ODB-0370), so its mark is cleared around the two
# steps and set again. A tap whose instance carries the clock on to clock pins
# of its own, such as a forwarding buffer that also clocks registers further
# on, would leave them without a clock while it is detached. Each output that
# carries the tap's clock to a register or macro clock pin, through whatever
# cells the timer's clock network runs through, gets a temporary clock with
# the period and waveform of the tap's clock, unless an SDC clock already
# starts on its net, so CTS builds that tree too. CTS would clone a movable
# cell that drives a root net as a clock gate, and a clone of a tapped cell
# would copy the detached input, so every tapped instance that drives clock
# pins, and every cell a temporary clock's net passes the clock through, is
# fixed during CTS. Everything is read from the timer before the first tap is
# detached: OpenSTA 26Q2 crashes searching a generated clock whose source path
# a detached pin broke. `reattach` deletes the temporary clocks and restores
# each fixed instance's placement status once.

namespace eval rb::cts_root_taps {
    # Each {iterm net_name was_dont_touch label}.
    variable taps {}
    # The temporary clocks `detach` created.
    variable temporary {}
    # {instance status} of each instance `detach` fixed, to restore.
    variable fixed {}
}

proc rb::cts_root_taps::label {iterm} {
    return "[::rb::insts::unescaped [[$iterm getInst] getName]]/[[$iterm getMTerm] getName]"
}

proc rb::cts_root_taps::sta_pin {iterm} {
    return [get_pins -quiet "[[$iterm getInst] getName]/[[$iterm getMTerm] getName]"]
}

# The names of the clocks the timer propagates to `pin`.
proc rb::cts_root_taps::clock_names {pin} {
    return [lmap clock [get_property $pin clocks] {get_name $clock}]
}

# Whether the timer propagates one of `clocks` (names) to `pin`, or with no
# names any clock.
proc rb::cts_root_taps::carries {pin clocks} {
    set names [clock_names $pin]
    if {[llength $clocks] == 0} {
        return [expr {[llength $names] > 0}]
    }
    foreach name $names {
        if {$name in $clocks} {
            return 1
        }
    }
    return 0
}

# Whether output `out` carries one of `clocks` (as `carries` takes them) to a
# register or macro clock pin, following the timer's clock network through any
# cell whose output carries it, as CTS finds its sinks. Asked while the taps
# are connected.
proc rb::cts_root_taps::reaches_sinks {out clocks} {
    set pin [sta_pin $out]
    if {$pin eq "" || ![carries $pin $clocks]} {
        return 0
    }
    set seen [dict create]
    set todo [list $out]
    while {[llength $todo] > 0} {
        set todo [lassign $todo driver]
        set net [$driver getNet]
        if {$net eq "NULL" || [dict exists $seen $net]} {
            continue
        }
        dict set seen $net 1
        foreach load [$net getITerms] {
            if {![$load isInputSignal]} {
                continue
            }
            set pin [sta_pin $load]
            if {$pin eq "" || ![carries $pin $clocks]} {
                continue
            }
            if {[get_property $pin is_register_clock] || [get_property $pin is_clock]} {
                return 1
            }
            foreach next [[$load getInst] getITerms] {
                if {[$next isOutputSignal] && [$next getNet] ne "NULL"} {
                    set next_pin [sta_pin $next]
                    if {$next_pin ne "" && [carries $next_pin $clocks]} {
                        lappend todo $next
                    }
                }
            }
        }
    }
    return 0
}

# The cells on the net of `out` that pass one of `clocks` on, as CTS would
# clone a clock gate on a root net: not when the clock reaches them through
# the tap, where they are not on a root net.
proc rb::cts_root_taps::gates {out clocks} {
    set result {}
    foreach load [[$out getNet] getITerms] {
        if {![$load isInputSignal]} {
            continue
        }
        set pin [sta_pin $load]
        if {$pin eq "" || ![carries $pin $clocks]
                || [get_property $pin is_register_clock] || [get_property $pin is_clock]} {
            continue
        }
        set cell [$load getInst]
        foreach next [$cell getITerms] {
            if {[$next isOutputSignal] && [$next getNet] ne "NULL"} {
                set next_pin [sta_pin $next]
                if {$next_pin ne "" && [carries $next_pin $clocks]
                        && [lsearch -exact $result $cell] < 0} {
                    lappend result $cell
                }
            }
        }
    }
    return $result
}

# The full names of every pin and port an SDC clock starts on.
proc rb::cts_root_taps::clock_sources {} {
    set sources [dict create]
    foreach clock [all_clocks] {
        foreach source [get_property $clock sources] {
            dict set sources [get_full_name $source] 1
        }
    }
    return $sources
}

# Whether an SDC clock starts on the net of `out`, at a pin or a port.
proc rb::cts_root_taps::has_source {out sources} {
    set net [get_nets -quiet -of_objects [sta_pin $out]]
    if {$net eq ""} {
        return 0
    }
    foreach object [concat [get_pins -quiet -of_objects $net] [get_ports -quiet -of_objects $net]] {
        if {[dict exists $sources [get_full_name $object]]} {
            return 1
        }
    }
    return 0
}

# A clock name `create_clock` would not redefine: `rb_cts_root_tap_<n>`, the
# first such name no clock has.
proc rb::cts_root_taps::unused_name {} {
    set index 0
    while {[llength [get_clocks -quiet rb_cts_root_tap_$index]] > 0} {
        incr index
    }
    return rb_cts_root_tap_$index
}

# Take every tap that `entries`, each {where spec}, names off its net. A spec
# that matches no pin, or a pin that is not an input on a clock net, fails the
# run, naming `where`.
proc rb::cts_root_taps::detach {entries} {
    variable taps
    variable temporary
    variable fixed
    set block [ord::get_db_block]
    set found {}
    foreach entry $entries {
        lassign $entry where spec
        set iterms [::rb::insts::pins $block $spec]
        if {[llength $iterms] == 0} {
            error "$where: no pin matches '$spec'"
        }
        foreach iterm $iterms {
            set name [label $iterm]
            set net [$iterm getNet]
            if {![$iterm isInputSignal]} {
                error "$where: $name is not an input pin"
            }
            set pin [sta_pin $iterm]
            if {$pin eq ""} {
                error "$where: the timer has no pin $name"
            }
            if {$net eq "NULL" || [llength [get_property $pin clocks]] == 0} {
                error "$where: $name is not on a clock net"
            }
            if {[lsearch -exact $found $iterm] < 0} {
                lappend found $iterm
            }
        }
    }

    # Plan from the timer while every tap is connected: per tap its clock's
    # period and waveform, per instance the outputs that need a temporary clock
    # and whether it drives a CTS root.
    set sources [clock_sources]
    set plans {}
    set roots [dict create]
    foreach iterm $found {
        set inst [$iterm getInst]
        set clocks [clock_names [sta_pin $iterm]]
        set clock [lindex [get_property [sta_pin $iterm] clocks] 0]
        # The clock's waveform in the SDC's time unit: the Clock object gives
        # seconds, `get_property` the period in the SDC's unit.
        set period [get_property $clock period]
        set waveform [lmap edge [$clock waveform] {
            expr {$edge * $period / [$clock period]}
        }]
        set outs {}
        foreach out [$inst getITerms] {
            if {![$out isOutputSignal] || [$out getNet] eq "NULL"} {
                continue
            }
            if {[reaches_sinks $out {}]} {
                dict set roots $inst 1
            }
            if {![has_source $out $sources] && [reaches_sinks $out $clocks]} {
                lappend outs $out
                foreach cell [gates $out $clocks] {
                    dict set roots $cell 1
                }
            }
        }
        lappend plans [list $iterm $period $waveform $outs]
    }

    set taps {}
    set temporary {}
    set fixed {}
    foreach inst [dict keys $roots] {
        if {![$inst isFixed]} {
            lappend fixed [list $inst [$inst getPlacementStatus]]
            $inst setPlacementStatus FIRM
        }
    }
    set clocked [dict create]
    foreach plan $plans {
        lassign $plan iterm period waveform outs
        set inst [$iterm getInst]
        set net [[$iterm getNet] getName]
        set dont_touch [$inst isDoNotTouch]
        $inst setDoNotTouch 0
        $iterm disconnect
        $inst setDoNotTouch $dont_touch
        set created {}
        foreach out $outs {
            if {[dict exists $clocked $out]} {
                continue
            }
            dict set clocked $out 1
            set temp [unused_name]
            create_clock -name $temp -period $period -waveform $waveform [sta_pin $out]
            set_propagated_clock [get_clocks $temp]
            lappend temporary $temp
            lappend created $temp
        }
        lappend taps [list $iterm $net $dont_touch [label $iterm]]
        set extra [expr {[llength $created] > 0 ? ", temporary clock [join $created {, }] on its output" : ""}]
        puts ">>> rb: CTS root tap [label $iterm] detached from net $net$extra"
    }
}

# Put each tap back on the net it came from, mark its instance dont-touch
# again if it was, restore the placement status of each instance `detach`
# fixed, and delete the temporary clocks. A net that no longer exists fails
# the run.
proc rb::cts_root_taps::reattach {tag} {
    variable taps
    variable temporary
    variable fixed
    set block [ord::get_db_block]
    foreach tap $taps {
        lassign $tap iterm name dont_touch label
        set net [$block findNet $name]
        if {$net eq "NULL" || $net eq ""} {
            error "cts-root-taps: net $name of $label no longer exists after CTS"
        }
        set inst [$iterm getInst]
        $inst setDoNotTouch 0
        $iterm connect $net
        $inst setDoNotTouch $dont_touch
        puts ">>> rb: CTS root tap $label on net $name"
        puts "$tag $label $name"
    }
    foreach entry $fixed {
        lassign $entry inst status
        $inst setPlacementStatus $status
    }
    foreach temp $temporary {
        delete_clock [get_clocks $temp]
    }
    set taps {}
    set temporary {}
    set fixed {}
}
