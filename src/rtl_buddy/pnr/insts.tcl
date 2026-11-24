# Instance names for the rb pnr OpenROAD flow's run keys that name instances
# (`dont-touch`, `cts-root-taps`), as `floorplan.macros` names them.
#
# Substituted into pnr.tcl ahead of the procedures that use it. It reads only
# the `findInst`, `getInsts` and `getName` of the block and its instances, so
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
