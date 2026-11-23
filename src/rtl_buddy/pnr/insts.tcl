# Instance and pin names for the rb pnr OpenROAD flow's run keys that name
# them (`dont-touch`, `cts-root-taps`), instances as `floorplan.macros` names
# them.
#
# Substituted into pnr.tcl ahead of the procedures that use it. It reads only
# the block, its instances and their terminals through the database API, so
# tests run it under a bare Tcl interpreter against a mock block.

namespace eval rb::insts {}

# `name` with the backslashes OpenROAD puts before brackets removed: a
# netlist's `g_bank[0].u_buf` is `g_bank\[0\].u_buf` in the database.
proc rb::insts::unescaped {name} {
    return [string map {"\\[" "[" "\\]" "]"} $name]
}

# The instances `pattern` names: the instance of that name, else every
# instance whose name matches it with `*` and `?` the only wildcards and
# brackets literal. The database's escaped spelling names the same instances.
proc rb::insts::match {block pattern} {
    set plain [unescaped $pattern]
    foreach name [list $pattern [string map {"[" "\\[" "]" "\\]"} $plain]] {
        set inst [$block findInst $name]
        if {$inst ne "NULL" && $inst ne ""} {
            return [list $inst]
        }
    }
    set glob [string map {"\\" "\\\\" "[" "\\[" "]" "\\]"} $plain]
    set insts {}
    foreach inst [$block getInsts] {
        if {[string match $glob [unescaped [$inst getName]]]} {
            lappend insts $inst
        }
    }
    return $insts
}

# The instance terminals `spec`, `<instance>/<pin>`, names: the instance part
# as `match` takes it, the pin part a pin name or a glob over the instance's
# pin names. Supply pins are left out.
proc rb::insts::pins {block spec} {
    set slash [string last / $spec]
    set inst_pattern [string range $spec 0 [expr {$slash - 1}]]
    set pin_pattern [string range $spec [expr {$slash + 1}] end]
    set iterms {}
    foreach inst [match $block $inst_pattern] {
        foreach iterm [$inst getITerms] {
            set mterm [$iterm getMTerm]
            if {[$mterm getSigType] ni {POWER GROUND}
                    && [string match $pin_pattern [$mterm getName]]} {
                lappend iterms $iterm
            }
        }
    }
    return $iterms
}
