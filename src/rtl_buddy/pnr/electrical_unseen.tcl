# The post-route electrical round's violators that its `repair_design` could
# not see (see `_post_route_extract_script`). `repair_design` finds violators
# on the timer's parasitics, the round's SPEF, but sizes the buffering that
# repairs them on Steiner trees at the placement wire RC, so a net whose
# extraction exceeds that estimate can be left as it is. `rb::unseen::report`
# runs at the end of the round's extraction: it lists the max-slew and
# max-capacitance violators on the extracted parasitics, estimates placement
# parasitics in their place, and prints each violator that is within its limit
# less the round's margin on that estimate.
#
# `parse` takes and returns plain strings, so tests run it under a bare Tcl
# interpreter.
namespace eval rb::unseen {}

# The rows of a `report_check_types -max_slew -max_capacitance` report as a list
# of {check pin limit value}, `check` being `slew` or `capacitance`, keeping
# each pin's row with the smallest slack.
proc rb::unseen::parse {text} {
    set check ""
    set number {-?[0-9]+(?:\.[0-9]*)?(?:[eE][-+]?[0-9]+)?}
    set row "^(\\S+)(?:\\s+\[A-Za-z_\]\\S*)?\\s+($number)\\s+($number)\\s+($number)\\s+\\((?:VIOLATED|MET)\\)"
    set worst [dict create]
    foreach line [split $text "\n"] {
        if {[regexp {^\s*max (slew|capacitance)\s*$} $line -> kind]} {
            set check $kind
            continue
        }
        if {[regexp {^\s*max fanout\s*$} $line]} {
            set check ""
            continue
        }
        if {$check eq "" || ![regexp $row $line -> pin limit value slack]} {
            continue
        }
        set key [list $check $pin]
        if {![dict exists $worst $key] || $slack < [lindex [dict get $worst $key] 1]} {
            dict set worst $key [list [list $check $pin $limit $value] $slack]
        }
    }
    set rows {}
    foreach {key entry} $worst {
        lappend rows [lindex $entry 0]
    }
    return $rows
}

proc rb::unseen::checks {args} {
    sta::redirect_string_begin
    set failed [catch {
        report_check_types -max_slew -max_capacitance -digits 6 {*}$args
    } err]
    set text [sta::redirect_string_end]
    if {$failed} {
        error $err
    }
    return [parse $text]
}

# Print `tag check pin limit extracted estimated` for each violator on the
# extracted parasitics that is within its limit less `slew_margin` or
# `cap_margin` (percent, as `repair_design` takes them) on the placement
# estimate. Replaces the session's parasitics with that estimate.
proc rb::unseen::report {tag slew_margin cap_margin} {
    set violators [checks -violators -max_count 100000]
    if {[llength $violators] == 0} {
        return
    }
    estimate_parasitics -placement
    set margins [dict create slew $slew_margin capacitance $cap_margin]
    foreach violator $violators {
        lassign $violator check name limit extracted
        set pin [get_pins -quiet $name]
        if {[llength $pin] == 0} {
            set pin [get_ports -quiet $name]
        }
        if {[llength $pin] == 0} {
            continue
        }
        set net [get_nets -quiet -of_objects $pin]
        if {[llength $net] == 0} {
            continue
        }
        set estimated ""
        foreach row [checks -net $net -max_count 100000] {
            lassign $row row_check row_pin row_limit row_value
            if {$row_check eq $check && $row_pin eq $name} {
                set estimated $row_value
            }
        }
        if {$estimated eq ""} {
            continue
        }
        set target [expr {$limit * (1.0 - [dict get $margins $check] / 100.0)}]
        if {$estimated <= $target} {
            puts "$tag $check $name $limit $extracted $estimated"
        }
    }
}
