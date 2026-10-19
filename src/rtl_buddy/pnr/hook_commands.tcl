
# Staged sessions but `finish`: a global Tcl command that a hook defines,
# renames, deletes or redefines exists only in its own session. Each hook but a
# `session` one is checked, and one that changed any is recorded as a
# `hook_commands` progress event, which rb turns into a warning.

proc rb::hook::commands {} {
    set out [dict create]
    # A SWIG object handle (`_<address>_p_<type>`) is a command per database
    # object a hook touches, not one it defines.
    foreach name [namespace eval :: {info commands}] {
        if {![regexp {^_[0-9a-f]+_p_} $name]} {
            dict set out $name {}
        }
    }
    foreach name [namespace eval :: {info procs}] {
        if {![regexp {^_[0-9a-f]+_p_} $name]} {
            dict set out $name [list [namespace eval :: [list info args $name]] \
                [namespace eval :: [list info body $name]]]
        }
    }
    return $out
}

rename rb::hook::run rb::hook::run_unchecked
proc rb::hook::run {point path} {
    if {$point eq "session"} {
        return [run_unchecked $point $path]
    }
    set before [commands]
    set code [catch {run_unchecked $point $path} result options]
    set after [commands]
    set changed {}
    dict for {name value} $after {
        if {![dict exists $before $name] || [dict get $before $name] ne $value} {
            lappend changed $name
        }
    }
    dict for {name value} $before {
        if {![dict exists $after $name]} { lappend changed $name }
    }
    if {[llength $changed] && [llength [info commands ::rb::ckpt::emit]]} {
        set names [lmap name [lsort $changed] {::rb::ckpt::json_str $name}]
        ::rb::ckpt::emit hook_commands [list point [::rb::ckpt::json_str $point] \
            path [::rb::ckpt::json_str $path] commands "\[[join $names {, }]\]"]
    }
    if {$code == 0} { return $result }
    return -options [dict incr options -level] $result
}
