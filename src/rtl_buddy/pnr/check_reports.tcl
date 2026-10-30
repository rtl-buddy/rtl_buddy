# Reads the timer's max-slew and max-capacitance checks for the `rb pnr` steps
# that act on them: the `placement.macro-pin-limits` step
# (`macro_pin_limits.tcl`) and the post-route electrical round's report
# (`electrical_unseen.tcl`). `parse` and `worst` take and return plain strings,
# so tests run them under a bare Tcl interpreter.

namespace eval rb::checks {}

# The checks of one `report_check_types -max_slew -max_capacitance` report, as
# a dict: `slew` and `capacitance`, each a list of {pin limit value scene}.
# Values are in the report's units, which are the load library's thresholds.
# `scene` is the row's scene when the report names one (`-verbose` on a
# design with several scenes), else empty.
proc rb::checks::parse {text} {
    set checks [dict create slew {} capacitance {}]
    set kind ""
    set number {-?[0-9]+(?:\.[0-9]*)?(?:[eE][-+]?[0-9]+)?}
    set pin ""
    foreach line [split $text "\n"] {
        if {[regexp {^\s*max (slew|capacitance|fanout)\s*$} $line -> section]} {
            set kind [expr {$section eq "fanout" ? "" : $section}]
            set pin ""
            continue
        }
        if {$kind eq ""} {
            continue
        }
        # `-verbose`: a pin line, the limit, the value, then the slack.
        if {[regexp {^Pin (\S+)\s*(?:[\^v]\s*)?(?:\(corner (\S+)\)\s*)?$} $line -> pin scene]} {
            set limit ""
            set value ""
            continue
        }
        if {$pin ne ""} {
            if {[regexp "^max $kind\\s+($number)" $line -> limit]} {
                continue
            }
            if {[regexp "^$kind\\s+($number)" $line -> value]} {
                continue
            }
            if {[regexp {^Slack\s} $line]} {
                if {$limit ne "" && $value ne ""} {
                    dict lappend checks $kind [list $pin $limit $value $scene]
                }
                set pin ""
            }
            continue
        }
        # Pin, an optional scene name, limit, value, slack, verdict.
        set row "^(\\S+)(?:\\s+(\[A-Za-z_\]\\S*))?\\s+($number)\\s+($number)\\s+($number)\\s+\\((?:VIOLATED|MET)\\)"
        if {[regexp $row $line -> name scene limit value slack]} {
            dict lappend checks $kind [list $name $limit $value $scene]
        }
    }
    return $checks
}

# The worst {limit value scene} of `pin` in `checks` for `kind`, by the
# smallest headroom, or an empty list when the report has no such check.
proc rb::checks::worst {checks kind pin} {
    set worst {}
    foreach row [dict get $checks $kind] {
        lassign $row name limit value scene
        if {$name ne $pin} {
            continue
        }
        if {$worst eq "" || $limit - $value < [lindex $worst 0] - [lindex $worst 1]} {
            set worst [list $limit $value $scene]
        }
    }
    return $worst
}

# The parsed checks of `report_check_types -max_slew -max_capacitance`, run
# with `args` (such as `-net`, `-violators`, `-verbose`, `-corner`) and every
# row it has.
proc rb::checks::report {args} {
    sta::redirect_string_begin
    set failed [catch {
        report_check_types -max_slew -max_capacitance -max_count 100000 -digits 6 {*}$args
    } err]
    set text [sta::redirect_string_end]
    if {$failed} {
        error $err
    }
    return [parse $text]
}
