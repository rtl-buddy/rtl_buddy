
# Sessions a later session follows (a staged run's but `finish`, and a
# one-session run's first session and post-route repair rounds): a global Tcl
# command that a hook defines, renames, deletes or redefines exists only in its
# own session. Each hook but a
# `session` one is checked, and one that changed any is recorded as a
# `hook_commands` progress event: `changed` names the commands that existed
# before the hook, which later sessions have in their original form, and
# `added` names the new ones, which later sessions lack.

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
    set added {}
    dict for {name value} $after {
        if {![dict exists $before $name]} {
            lappend added $name
        } elseif {[dict get $before $name] ne $value} {
            lappend changed $name
        }
    }
    dict for {name value} $before {
        if {![dict exists $after $name]} { lappend changed $name }
    }
    if {[llength $changed] + [llength $added] && [llength [info commands ::rb::ckpt::emit]]} {
        set fields [list point [::rb::ckpt::json_str $point] \
            path [::rb::ckpt::json_str $path]]
        foreach key {changed added} {
            set names [lmap name [lsort [set $key]] {::rb::ckpt::json_str $name}]
            lappend fields $key "\[[join $names {, }]\]"
        }
        ::rb::ckpt::emit hook_commands $fields
    }
    if {$code == 0} { return $result }
    return -options [dict incr options -level] $result
}
