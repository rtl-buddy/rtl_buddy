# The `dont-touch` run key for the rb pnr OpenROAD flow.
#
# Substituted into pnr.tcl after insts.tcl, only when the run sets the key.
# `apply` marks the instances dont-touch at the start of the floorplan, so the
# floorplan's setup repair and every later step leave them alone.
# `global_connect` skips a dont-touch instance (ODB-0383), and once its
# supplies are skipped nothing connects them later, so `lift` clears the mark
# just before the tap cells and the power grid and `restore` connects the
# supplies and sets it again just after, before the `after-pdn` hooks. The mark
# is kept in the database, so a checkpoint and every later session keep it.

namespace eval rb::dont_touch {
    # The instances `apply` marked, for `lift` and `restore`.
    variable insts {}
}

# Mark dont-touch every instance that `entries`, each {where pattern}, names.
# A pattern that matches no instance fails the run, naming `where`.
proc rb::dont_touch::apply {entries} {
    variable insts
    set block [ord::get_db_block]
    set insts {}
    foreach entry $entries {
        lassign $entry where pattern
        set matched [::rb::insts::match $block $pattern]
        if {[llength $matched] == 0} {
            error "$where: no instance matches '$pattern'"
        }
        foreach inst $matched {
            $inst setDoNotTouch 1
        }
        lappend insts {*}$matched
        puts ">>> rb: dont-touch $pattern: [llength $matched] instance(s)"
    }
    set insts [lsort -unique $insts]
}

proc rb::dont_touch::lift {} {
    variable insts
    foreach inst $insts {
        $inst setDoNotTouch 0
    }
}

# Connect the supplies of the instances `apply` marked, then mark them again.
# With global connection rules, an instance left with a supply pin on no net
# fails the run: no rule matches the pin, and once the instance is dont-touch
# no later rule can connect it.
proc rb::dont_touch::restore {} {
    variable insts
    ::rb::global_connect
    if {[llength [[ord::get_db_block] getGlobalConnects]] > 0} {
        foreach inst $insts {
            foreach iterm [$inst getITerms] {
                set mterm [$iterm getMTerm]
                if {[$mterm getSigType] in {POWER GROUND} && [$iterm getNet] eq "NULL"} {
                    error "dont-touch: [$inst getName] supply pin [$mterm getName]\
                        is on no net after the power grid's global connections;\
                        add a global connection rule for it"
                }
            }
        }
    }
    foreach inst $insts {
        $inst setDoNotTouch 1
    }
}
