# The post-route electrical round's violators that its `repair_design` could
# not see (see `_unseen_tcl`). `repair_design` finds violators on the timer's
# parasitics, the round's SPEF, but sizes the buffering that repairs them on
# Steiner trees at the placement wire RC, so a net whose extraction exceeds
# that estimate can be left as it is. `rb::unseen::report` runs at the end of
# the round's extraction: it reads the max-slew and max-capacitance violators
# on the extracted parasitics (`check_reports.tcl`), estimates placement
# parasitics in their place, and prints each violator that is within its limit
# less the round's margin on that estimate, read from the same check at the
# scene its extracted row names.
#
# It leaves out the violators `repair_design` (OpenROAD 26Q4) does not repair
# whatever the parasitics: a port, and a pin on a net that is driven by a
# constant, carries a clock, or is dont_touch, special or connected by
# abutment.
namespace eval rb::unseen {}

# Whether `method` of the database object `db` answers 1; 0 when it does not
# or cannot answer.
proc rb::unseen::flag {db method} {
    return [expr {![catch {{*}$db $method} value] && $value eq "1"}]
}

# Whether `repair_design` leaves the pin `pin` on `net` alone whatever its
# parasitics.
proc rb::unseen::skipped {pin net} {
    if {[sta::is_clock $pin] eq "1"} {
        return 1
    }
    # Tie cells and other constant drivers, by their simulated logic value.
    foreach driver [sta::net_driver_pins $net] {
        if {[sta::pin_sim_logic_value $driver] in {0 1}} {
            return 1
        }
    }
    set db [sta::sta_to_db_net $net]
    if {$db eq "" || $db eq "NULL"} {
        return 1
    }
    foreach method {isDoNotTouch isSpecial isConnectedByAbutment} {
        if {[flag $db $method]} {
            return 1
        }
    }
    return 0
}

# Print `tag check pin limit extracted estimated` for each violator on the
# extracted parasitics that is within its limit less `slew_margin` or
# `cap_margin` (percent, as `repair_design` takes them) on the placement
# estimate. Replaces the session's parasitics with that estimate.
proc rb::unseen::report {tag slew_margin cap_margin} {
    set checks [::rb::checks::report -violators -verbose]
    set violators {}
    foreach check {slew capacitance} {
        set pins {}
        foreach row [dict get $checks $check] {
            set name [lindex $row 0]
            if {$name ni $pins} {
                lappend pins $name
                lappend violators [list $check $name {*}[::rb::checks::worst $checks $check $name]]
            }
        }
    }
    if {[llength $violators] == 0} {
        return
    }
    estimate_parasitics -placement
    set margins [dict create slew $slew_margin capacitance $cap_margin]
    # Per net: whether `repair_design` skips it; per net and scene: its
    # checks on the estimate.
    set skips [dict create]
    set estimates [dict create]
    foreach violator $violators {
        lassign $violator check name limit extracted scene
        set pin [get_pins -quiet $name]
        if {[llength $pin] == 0} {
            continue
        }
        set net [get_nets -quiet -of_objects $pin]
        if {[llength $net] == 0} {
            continue
        }
        set key [get_full_name $net]
        if {![dict exists $skips $key]} {
            dict set skips $key [skipped $pin $net]
        }
        if {[dict get $skips $key]} {
            continue
        }
        if {![dict exists $estimates $key $scene]} {
            set options [list -net $net -verbose]
            if {$scene ne ""} {
                lappend options -corner $scene
            }
            dict set estimates $key $scene [::rb::checks::report {*}$options]
        }
        set estimate [::rb::checks::worst [dict get $estimates $key $scene] $check $name]
        if {[llength $estimate] == 0} {
            continue
        }
        set estimated [lindex $estimate 1]
        set target [expr {$limit * (1.0 - [dict get $margins $check] / 100.0)}]
        if {$estimated <= $target} {
            puts [format "%s %s %s %s %s %s" $tag $check $name $limit $extracted $estimated]
        }
    }
}
