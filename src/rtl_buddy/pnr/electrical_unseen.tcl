# The post-route electrical round's violators that its `repair_design` could
# not see (see `_unseen_tcl`). `repair_design` finds violators on the timer's
# parasitics, the round's SPEF, but sizes the buffering that repairs them on
# Steiner trees at the placement wire RC, so a net whose extraction exceeds
# that estimate can be left as it is. `rb::unseen::report` runs at the end of
# the round's extraction: it reads the max-slew and max-capacitance violators
# on the extracted parasitics (`check_reports.tcl`), estimates placement
# parasitics in their place, and prints each violator that is within its limit
# less the round's margin on that estimate.
#
# It leaves out the violators `repair_design` (OpenROAD 26Q4) does not repair
# whatever the parasitics: a port, and a pin on a clock net or on a net that is
# dont_touch, special or connected by abutment.
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

# The worst capacitance over the scenes that `net` loads its driver with, in
# the report's unit.
proc rb::unseen::net_capacitance {net} {
    set worst 0.0
    foreach scene [sta::scenes] {
        set cap [sta::Net_capacitance $net $scene max]
        if {$cap > $worst} {
            set worst $cap
        }
    }
    return [sta::capacitance_sta_ui $worst]
}

# Print `tag check pin limit extracted estimated` for each violator on the
# extracted parasitics that is within its limit less `slew_margin` or
# `cap_margin` (percent, as `repair_design` takes them) on the placement
# estimate. Replaces the session's parasitics with that estimate.
proc rb::unseen::report {tag slew_margin cap_margin} {
    set checks [::rb::checks::report -violators]
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
    # Per net: whether `repair_design` skips it, and its estimated load.
    set nets [dict create]
    foreach violator $violators {
        lassign $violator check name limit extracted
        set pin [get_pins -quiet $name]
        if {[llength $pin] == 0} {
            continue
        }
        set net [get_nets -quiet -of_objects $pin]
        if {[llength $net] == 0} {
            continue
        }
        set key [get_full_name $net]
        if {![dict exists $nets $key]} {
            dict set nets $key [dict create skipped [skipped $pin $net]]
        }
        if {[dict get $nets $key skipped]} {
            continue
        }
        if {$check eq "slew"} {
            set estimated [get_property $pin slew_max]
        } else {
            if {![dict exists $nets $key cap]} {
                dict set nets $key cap [net_capacitance $net]
            }
            set estimated [dict get $nets $key cap]
        }
        if {$estimated eq ""} {
            continue
        }
        set target [expr {$limit * (1.0 - [dict get $margins $check] / 100.0)}]
        if {$estimated <= $target} {
            puts [format "%s %s %s %s %s %.6f" $tag $check $name $limit $extracted $estimated]
        }
    }
}
