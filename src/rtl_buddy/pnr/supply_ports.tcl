# Supply port re-assertion for the `rb pnr` OpenROAD flow.
#
# Substituted into pnr.tcl ahead of the final reports, in the `finish` session.
# OpenSTA fixes a top port's direction when the port's bterm is created, and the
# power grid generator sets a supply bterm's signal type only afterwards, so in
# the session that built the grid VDD and VSS stay bidirectional ports: the
# routed netlist declares them and a hardened block's timing model lists them as
# `inout` pins. A session that reads the database makes them power and ground
# ports. Setting the IO type again re-derives the port's direction from the
# signal type. The timing graph takes each pin's direction from the database
# already, so it is left alone: rebuilding it moves the timing model.

namespace eval rb::supply_ports {}

proc rb::supply_ports::reassert {} {
    foreach bterm [[ord::get_db_block] getBTerms] {
        set sig_type [$bterm getSigType]
        if {$sig_type eq "POWER" || $sig_type eq "GROUND"} {
            $bterm setIoType [$bterm getIoType]
        }
    }
}
