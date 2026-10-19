# Run-level Tcl hooks for the rb pnr flow.
#
# Substituted into the generated `pnr.tcl` only when pnr.yaml sets `hooks:`.
# `rb::hook::run` sources one hook file at Tcl global scope, so the flow's
# variables ($OUT_DIR, $DESIGN, $block, $macros) are visible to it. When stage
# checkpoints are armed it brackets the file with `hook_begin` and `hook_end`
# progress events.

namespace eval rb::hook {}

# `extra` is a flat list of key/value pairs; values are JSON strings except
# `elapsed_s`, a number. Nothing is written unless stage checkpoints are armed.
proc rb::hook::emit {event point path {extra {}}} {
    if {![llength [info commands ::rb::ckpt::emit]]} { return }
    set fields [list point [::rb::ckpt::json_str $point] path [::rb::ckpt::json_str $path]]
    foreach {key value} $extra {
        lappend fields $key [expr {$key eq "elapsed_s" ? $value : [::rb::ckpt::json_str $value]}]
    }
    ::rb::ckpt::emit $event $fields
}

proc rb::hook::run {point path} {
    set begin [clock milliseconds]
    emit hook_begin $point $path
    # Stage checkpoints mark the flow's own commands; a command a hook runs
    # does not move a stage boundary.
    set anchors {}
    if {[info exists ::rb::ckpt::anchors]} {
        set anchors $::rb::ckpt::anchors
        set ::rb::ckpt::anchors {}
    }
    set code [catch {uplevel #0 [list source $path]} message options]
    if {[info exists ::rb::ckpt::anchors]} {
        set ::rb::ckpt::anchors $anchors
    }
    set elapsed [format %.3f [expr {([clock milliseconds] - $begin) / 1000.0}]]
    if {$code == 0} {
        emit hook_end $point $path [list status ok elapsed_s $elapsed]
        return
    }
    if {$code == 3 || $code == 4} {
        set message "invoked [lindex {- - - break continue} $code] outside of a loop"
        set options [dict create -code error -level 0 -errorinfo $message]
        set code 1
    }
    emit hook_end $point $path [list status error elapsed_s $elapsed error $message]
    if {$code == 1} {
        # OpenROAD prints errorInfo, not the message: lead it with the prefix
        # and keep the original trace under it.
        set prefixed "Hook $point ($path): $message"
        set info [dict get $options -errorinfo]
        if {$message eq "" || [string first $message $info] == 0} {
            set info "$prefixed[string range $info [string length $message] end]"
        }
        dict set options -errorinfo $info
        set message $prefixed
    }
    return -options $options $message
}
