# The latch borrow report of the rb pnr OpenROAD flow.
#
# Substituted into the final reports, on the final parasitics. OpenSTA gives a
# latch data pin whose data arrives while the latch is open a setup slack of
# exactly 0 and hands the borrowed time on to the paths that start at the
# latch; only `report_checks` prints how much of the latch's window is left,
# under "Time Borrowing Information". For each corner this appends the setup
# check of every latch data pin, one path each, to the report file rb reads,
# after a header line naming the corner, then prints `tag corner pins`. A
# design without latches writes and prints nothing. Every step is caught, so a
# failure loses only that corner's report.

namespace eval rb::latch_borrow {}

# Write the report to `file` and print the tag lines for `corners`, each
# {name report_checks_args ?enter?}: `enter`, when given, is run before the
# corner is reported, such as making its SDC mode current, and `leave`, when
# not empty, after the last corner.
proc rb::latch_borrow::report {tag header file corners leave} {
    file delete -force $file
    if {[catch {set pins [all_registers -level_sensitive -data_pins]} err]} {
        puts "rb: latch borrow report unavailable: $err"
        return
    }
    if {[llength $pins] == 0} {
        return
    }
    foreach corner $corners {
        lassign $corner corner_name corner_args enter
        if {[catch {
            if {$enter ne ""} {
                uplevel #0 $enter
            }
            set out [open $file a]
            puts $out "$header $corner_name"
            close $out
            report_checks -path_delay max -to $pins -format full -digits 6 \
                -group_path_count [llength $pins] -endpoint_path_count 1 \
                {*}$corner_args >> $file
            puts "$tag $corner_name [llength $pins]"
        } err]} {
            puts "rb: latch borrow report at $corner_name unavailable: $err"
        }
    }
    if {$leave ne ""} {
        uplevel #0 $leave
    }
}
