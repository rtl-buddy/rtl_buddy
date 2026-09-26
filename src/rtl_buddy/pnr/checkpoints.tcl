# Stage checkpoints and progress events for the rb pnr flow (#653).
#
# Substituted into the generated `pnr.tcl` only when the run sets
# `checkpoints:` in pnr.yaml; a run that does not renders the flow without
# any of this. Pure Tcl apart from the OpenROAD writers it calls, so it can
# be driven under a bare Tcl interpreter with stub commands.
#
# Nothing here edits the flow's own stage sequence. Progress and checkpoints
# hang off Tcl execution traces on the flow's commands instead: a step's
# begin/end is the enter/leave of its command, and a checkpoint is written
# on the way into the first command of the *next* stage — so a checkpoint
# always holds exactly the database the stage before it left, and the flow
# template carries one substitution point instead of one per stage.
#
# Events go to a JSON-lines file, appended and closed per event, so the
# file is complete up to the last event even when the process is killed
# mid-stage: the last `step_begin` without a matching `step_end` names the
# step a wall limit interrupted.

namespace eval rb::ckpt {
    variable dir ""
    variable progress ""
    # stage name -> {index command when}, from `arm`.
    variable anchors {}
    # Stages already written, so a command the flow calls twice (the
    # detailed placer runs three times) checkpoints once.
    variable done {}
    variable seq 0
    # command -> list of begin times (ms), a stack for re-entrant calls.
    variable started {}
}

# A JSON string literal. Error messages carry quotes, backslashes and
# newlines, and a progress line that is not valid JSON is worse than none.
proc rb::ckpt::json_str {s} {
    set out ""
    foreach ch [split $s ""] {
        scan $ch %c code
        if {$ch eq "\""} {
            append out "\\\""
        } elseif {$ch eq "\\"} {
            append out "\\\\"
        } elseif {$code < 0x20} {
            append out [format "\\u%04x" $code]
        } else {
            append out $ch
        }
    }
    return "\"$out\""
}

# Append one event. `fields` is a flat list of key/already-encoded-value
# pairs. Opened and closed per event so every line is on disk as soon as it
# is written; a failure to write is reported once on stdout and otherwise
# ignored — progress is a debugging aid and must never fail the flow.
proc rb::ckpt::emit {event fields} {
    variable progress
    variable seq
    incr seq
    set ms [clock milliseconds]
    set t [clock format [expr {$ms / 1000}] -format "%Y-%m-%dT%H:%M:%S%z"]
    # ISO 8601 offset (+08:00), the spelling the Python-side events use.
    regsub {([+-][0-9][0-9])([0-9][0-9])$} $t {\1:\2} t
    set parts [list "\"event\": [json_str $event]" "\"seq\": $seq" \
        "\"t\": [json_str $t]" "\"epoch_ms\": $ms"]
    foreach {k v} $fields {
        lappend parts "[json_str $k]: $v"
    }
    if {[catch {
        set fh [open $progress a]
        puts $fh "\{[join $parts {, }]\}"
        close $fh
    } err]} {
        puts ">>> rb checkpoints: cannot append progress event: $err"
    }
}

proc rb::ckpt::on_enter {cmd_string op} {
    variable started
    set cmd [lindex $cmd_string 0]
    dict lappend started $cmd [clock milliseconds]
    checkpoint_for $cmd enter
    emit step_begin [list step [json_str $cmd]]
}

proc rb::ckpt::on_leave {cmd_string code result op} {
    variable started
    set cmd [lindex $cmd_string 0]
    set elapsed null
    if {[dict exists $started $cmd]} {
        set stack [dict get $started $cmd]
        set begin [lindex $stack end]
        dict set started $cmd [lrange $stack 0 end-1]
        set elapsed [format %.3f [expr {([clock milliseconds] - $begin) / 1000.0}]]
    }
    if {$code == 0 || $code == 2} {
        emit step_end [list step [json_str $cmd] status [json_str ok] \
            elapsed_s $elapsed]
        checkpoint_for $cmd leave
    } else {
        emit step_end [list step [json_str $cmd] status [json_str error] \
            elapsed_s $elapsed error [json_str $result]]
    }
}

proc rb::ckpt::on_exit {cmd_string op} {
    set code [lindex $cmd_string 1]
    if {$code eq ""} { set code 0 }
    emit flow_end [list exit_code $code]
}

# Write the checkpoint anchored at this command and edge, if one is and it
# has not been written yet.
proc rb::ckpt::checkpoint_for {cmd when} {
    variable anchors
    variable done
    dict for {stage spec} $anchors {
        lassign $spec index anchor_cmd anchor_when
        if {$anchor_cmd ne $cmd || $anchor_when ne $when} { continue }
        if {[lsearch -exact $done $stage] >= 0} { continue }
        lappend done $stage
        write_checkpoint $index $stage
    }
}

# One checkpoint: ODB (the database a resume re-enters), DEF (what KLayout
# stream-out reads, so `rb pnr-export --checkpoint` needs no OpenROAD), the
# SDC as the flow has it at this point, and — after global routing — the
# route guides and the raw global-route segments. The segments are what
# `estimate_parasitics -global_routing` needs; guides alone do not restore
# them (`read_guides` warns it gives no parasitics).
#
# The `checkpoint` event is written only after every file is on disk, so a
# checkpoint without one — a kill mid-write — is not a checkpoint.
proc rb::ckpt::write_checkpoint {index stage} {
    variable dir
    global DESIGN
    set base [file join $dir "${index}_${stage}"]
    set files [list odb "$base.odb" def "$base.def" sdc "$base.sdc"]
    if {$stage eq "global_route"} {
        lappend files guides "$base.guide" segments "$base.segments"
    }
    puts ">>> Checkpoint ${index}_${stage}"
    emit checkpoint_begin [list stage [json_str $stage] index [json_str $index]]
    if {[catch {
        file mkdir $dir
        foreach {kind path} $files {
            switch -- $kind {
                odb      { write_db $path }
                def      { write_def $path }
                sdc      { write_sdc $path }
                guides   { write_guides $path }
                segments { write_global_route_segments $path }
            }
        }
    } err]} {
        puts ">>> rb checkpoints: ${index}_${stage} not written: $err"
        emit checkpoint [list stage [json_str $stage] index [json_str $index] \
            status [json_str error] error [json_str $err]]
        return
    }
    set encoded {}
    foreach {kind path} $files {
        lappend encoded "[json_str $kind]: [json_str [file tail $path]]"
    }
    emit checkpoint [list stage [json_str $stage] index [json_str $index] \
        status [json_str ok] design [json_str $DESIGN] \
        files "\{[join $encoded {, }]\}"]
}

# Arm progress + checkpoints. `steps` are the flow commands traced for
# progress; `anchors` maps each requested stage to {index command enter|leave}.
proc rb::ckpt::arm {ckpt_dir progress_file steps stage_anchors} {
    variable dir $ckpt_dir
    variable progress $progress_file
    variable anchors $stage_anchors
    file mkdir $dir
    set traced {}
    foreach cmd $steps {
        if {[info commands $cmd] eq ""} { continue }
        trace add execution $cmd enter rb::ckpt::on_enter
        trace add execution $cmd leave rb::ckpt::on_leave
        lappend traced [json_str $cmd]
    }
    trace add execution exit enter rb::ckpt::on_exit
    set staged {}
    dict for {stage spec} $anchors { lappend staged [json_str $stage] }
    emit flow_begin [list steps "\[[join $traced {, }]\]" \
        checkpoints "\[[join $staged {, }]\]"]
}
