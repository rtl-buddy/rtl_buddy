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
# steps and set again. A tap whose instance drives clock sinks of its own, such
# as a forwarding buffer that also clocks registers further on, would leave
# those sinks without a clock while it is detached; such an instance gets a
# temporary clock on its output, with the period and waveform of the tap's
# clock, so CTS builds that tree too. `reattach` deletes it.

namespace eval rb::cts_root_taps {
    # Each {iterm net_name was_dont_touch temporary_clocks placement_status label}.
    variable taps {}
}

proc rb::cts_root_taps::label {iterm} {
    return "[::rb::insts::unescaped [[$iterm getInst] getName]]/[[$iterm getMTerm] getName]"
}

proc rb::cts_root_taps::sta_pin {iterm} {
    return [get_pins -quiet "[[$iterm getInst] getName]/[[$iterm getMTerm] getName]"]
}

# Whether an output of `inst` reaches a register or macro clock pin through
# buffers and inverters alone, as CTS traces a clock.
proc rb::cts_root_taps::drives_sinks {inst} {
    set seen [dict create]
    set todo [list $inst]
    while {[llength $todo] > 0} {
        set todo [lassign $todo cell]
        foreach iterm [$cell getITerms] {
            set net [$iterm getNet]
            if {![$iterm isOutputSignal] || $net eq "NULL" || [dict exists $seen $net]} {
                continue
            }
            dict set seen $net 1
            foreach load [$net getITerms] {
                if {![$load isInputSignal]} {
                    continue
                }
                set pin [sta_pin $load]
                if {$pin eq ""} {
                    continue
                }
                if {[get_property $pin is_register_clock] || [get_property $pin is_clock]} {
                    return 1
                }
                set lib_cell [get_property [get_cells -of_objects $pin] liberty_cell]
                if {$lib_cell ne "" && ([get_property $lib_cell is_buffer]
                        || [get_property $lib_cell is_inverter])} {
                    lappend todo [$load getInst]
                }
            }
        }
    }
    return 0
}

# Take every tap that `entries`, each {where spec}, names off its net. A spec
# that matches no pin, or a pin that is not an input on a clock net, fails the
# run, naming `where`.
proc rb::cts_root_taps::detach {entries} {
    variable taps
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
    set taps {}
    set index 0
    foreach iterm $found {
        set inst [$iterm getInst]
        set net [[$iterm getNet] getName]
        set clock [lindex [get_property [sta_pin $iterm] clocks] 0]
        set sinks [drives_sinks $inst]
        set dont_touch [$inst isDoNotTouch]
        $inst setDoNotTouch 0
        $iterm disconnect
        $inst setDoNotTouch $dont_touch
        set temporary {}
        set status [$inst getPlacementStatus]
        if {$sinks} {
            # The clock's waveform in the SDC's time unit: the Clock object
            # gives seconds, `get_property` the period in the SDC's unit.
            set period [get_property $clock period]
            set waveform [lmap edge [$clock waveform] {
                expr {$edge * $period / [$clock period]}
            }]
            foreach out [$inst getITerms] {
                if {![$out isOutputSignal] || [$out getNet] eq "NULL"} {
                    continue
                }
                set out_pin [sta_pin $out]
                if {[llength [get_property $out_pin clocks]] > 0} {
                    continue
                }
                set temp rb_cts_root_tap_$index
                incr index
                create_clock -name $temp -period $period -waveform $waveform $out_pin
                set_propagated_clock [get_clocks $temp]
                lappend temporary $temp
            }
            # CTS clones the instance that drives a clock net as a clock gate
            # unless it is fixed, and a clone would copy the detached input.
            if {[llength $temporary] > 0 && ![$inst isFixed]} {
                $inst setPlacementStatus FIRM
            }
        }
        lappend taps [list $iterm $net $dont_touch $temporary $status [label $iterm]]
        set extra [expr {[llength $temporary] > 0 ? ", temporary clock [join $temporary {, }] on its output" : ""}]
        puts ">>> rb: CTS root tap [label $iterm] detached from net $net$extra"
    }
}

# Put each tap back on the net it came from, mark its instance dont-touch
# again if it was, and delete the temporary clocks. A net that no longer
# exists fails the run.
proc rb::cts_root_taps::reattach {tag} {
    variable taps
    set block [ord::get_db_block]
    foreach tap $taps {
        lassign $tap iterm name dont_touch temporary status label
        set net [$block findNet $name]
        if {$net eq "NULL" || $net eq ""} {
            error "cts-root-taps: net $name of $label no longer exists after CTS"
        }
        set inst [$iterm getInst]
        $inst setDoNotTouch 0
        $iterm connect $net
        $inst setDoNotTouch $dont_touch
        $inst setPlacementStatus $status
        foreach temp $temporary {
            delete_clock [get_clocks $temp]
        }
        puts ">>> rb: CTS root tap $label on net $name"
        puts "$tag $label $name"
    }
    set taps {}
}
