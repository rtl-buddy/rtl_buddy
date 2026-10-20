# Stage checkpoints, stage resources and progress events for the rb pnr flow.
#
# Substituted into the generated script when pnr.yaml sets `checkpoints:` or
# `stages: true`, or `rb pnr` runs with `--stop-after`. Progress hangs off Tcl
# execution traces on the flow's commands; the flow's stage sequence is not
# edited.
#
# In a one-session run a stage ends on an edge of a traced flow command (its
# anchor) or where the flow calls `rb::ckpt::stage_end`. There the stage's wall
# time, CPU time and peak RSS are recorded and, when requested, its checkpoint
# is written, so the checkpoint holds the database the stage left. A staged
# session calls `rb::ckpt::stage_end` once, at its end; rb measures the session
# as a process.
#
# Events are appended to a JSON-lines file, opened and closed per event, so the
# file survives a kill: the last `step_begin` without a `step_end` names the
# interrupted step.
#
# A `stage_timing` event carries each corner's setup and hold worst slack and
# TNS, in ps, on the parasitics the stage's own report uses. A stage's end is
# timed in `stage_end`; the flow calls `stage_timing` and `timing_event` where
# a stage has another point to time.

namespace eval rb::ckpt {
    variable dir ""
    variable progress ""
    # stage -> {command enter|leave}: the traced edge that ends the stage.
    variable anchors {}
    # Stages whose checkpoint is written.
    variable wanted {}
    # stage -> file index, in flow order.
    variable order {}
    # Stages whose checkpoint carries the route guides and segments.
    variable routed {}
    # stage -> {kind file ...}: reports in $OUT_DIR a stage's checkpoint keeps.
    variable reports {}
    # Globals a staged checkpoint saves for the next session.
    variable state {}
    # 1: this session measures its stages; 0: rb measures the session.
    variable account 0
    # 1: a checkpoint that cannot be written fails the session.
    variable strict 0
    # stage -> {source command}: the parasitics `source` a stage's end is timed
    # on and the command that sets them ({} for none).
    variable timed {}
    # The corners to time, and whether they are STA scenes (a multi-corner
    # platform) or the session's one default scene.
    variable corners {}
    variable scenes 0
    # The stage/point pairs already timed.
    variable timed_done {}
    # Corners with an SDC mode each: `write_sdc` (rb::ckpt::write_sdc) also
    # writes each one's mode alone, to NN_<stage>.<corner>.sdc.
    variable modes {}
    # A stage ends once.
    variable done {}
    variable seq 0
    # command -> stack of begin times (ms)
    variable started {}
    # The stage being measured ("" for none), its start and the counters then.
    variable current ""
    variable begin_ms 0
    variable begin_cpu ""
    variable measured 0
    variable rss_reset 0
}

# JSON string literal; error messages carry quotes, backslashes and newlines.
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

# Append one event. `fields` is a flat list of key/already-encoded-value pairs.
# A write failure is reported on stdout and never fails the flow.
proc rb::ckpt::emit {event fields} {
    variable progress
    variable seq
    incr seq
    set ms [clock milliseconds]
    set t [clock format [expr {$ms / 1000}] -format "%Y-%m-%dT%H:%M:%S%z"]
    # ISO 8601 offset (+08:00), as in the Python-side events.
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
    end_stage [expr {$code == 0 ? "ok" : "error"}]
    emit flow_end [list exit_code $code]
}

# End the stage whose anchor is this command and edge, if it has not ended.
proc rb::ckpt::checkpoint_for {cmd when} {
    variable anchors
    dict for {stage anchor} $anchors {
        if {$anchor eq [list $cmd $when]} {
            stage_end $stage
        }
    }
}

# CPU seconds (user + system, every thread) of this process so far: fields 14
# and 15 of /proc/self/stat, in USER_HZ ticks, which Linux fixes at 100. ""
# without /proc.
proc rb::ckpt::cpu_seconds {} {
    if {[catch {
        set fh [open /proc/self/stat r]
        set text [read $fh]
        close $fh
    }]} {
        return ""
    }
    # The command name in field 2 may hold spaces; count from its closing paren.
    set rest [string range $text [expr {[string last ")" $text] + 2}] end]
    lassign [lrange $rest 11 12] utime stime
    if {![string is integer -strict $utime] || ![string is integer -strict $stime]} {
        return ""
    }
    return [expr {($utime + $stime) / 100.0}]
}

# Peak resident set size (VmHWM) in kB, or "" without /proc.
proc rb::ckpt::peak_rss_kb {} {
    if {[catch {
        set fh [open /proc/self/status r]
        set text [read $fh]
        close $fh
    }]} {
        return ""
    }
    if {[regexp -line {^VmHWM:\s+([0-9]+)\s+kB} $text -> kb]} {
        return $kb
    }
    return ""
}

# Restart the peak-RSS window (Linux 4.0+); 0 when the kernel refuses.
proc rb::ckpt::reset_peak_rss {} {
    if {[catch {
        set fh [open /proc/self/clear_refs WRONLY]
        puts -nonewline $fh 5
        close $fh
    }]} {
        return 0
    }
    return 1
}

proc rb::ckpt::begin_stage {stage} {
    variable account
    variable order
    variable current
    variable begin_ms
    variable begin_cpu
    variable rss_reset
    if {!$account || ![dict exists $order $stage]} {
        return
    }
    set current $stage
    set rss_reset [reset_peak_rss]
    set begin_cpu [cpu_seconds]
    set begin_ms [clock milliseconds]
    emit stage_begin [list stage [json_str $stage] \
        index [json_str [dict get $order $stage]]]
}

# Record the measured stage's wall time, CPU time and peak RSS. A peak that
# could not be reset since an earlier stage is unknown (null).
proc rb::ckpt::end_stage {status} {
    variable order
    variable current
    variable begin_ms
    variable begin_cpu
    variable measured
    variable rss_reset
    if {$current eq ""} {
        return
    }
    set wall [format %.3f [expr {([clock milliseconds] - $begin_ms) / 1000.0}]]
    set cpu [cpu_seconds]
    if {$cpu ne "" && $begin_cpu ne ""} {
        set cpu [format %.2f [expr {$cpu - $begin_cpu}]]
    } else {
        set cpu null
    }
    set rss [peak_rss_kb]
    if {$rss eq "" || (!$rss_reset && $measured > 0)} {
        set rss null
    }
    emit stage_end [list stage [json_str $current] \
        index [json_str [dict get $order $current]] status [json_str $status] \
        wall_s $wall cpu_s $cpu peak_rss_kb $rss measured_by [json_str session]]
    incr measured
    set current ""
}

# End `stage`: write its checkpoint when requested, record its resources and,
# with `next`, start measuring the stage after it. `stop` ends the flow here,
# so its checkpoint must be written.
proc rb::ckpt::stage_end {stage {then next}} {
    variable done
    variable wanted
    variable order
    variable strict
    if {[lsearch -exact $done $stage] >= 0} {
        return
    }
    lappend done $stage
    if {[lsearch -exact $wanted $stage] >= 0} {
        write_checkpoint [dict get $order $stage] $stage \
            [expr {$strict || $then eq "stop"}]
    }
    stage_timing $stage stage_end
    end_stage ok
    if {$then eq "next"} {
        set stages [dict keys $order]
        set next [lindex $stages [expr {[lsearch -exact $stages $stage] + 1}]]
        if {$next ne ""} {
            begin_stage $next
        }
    }
}

# Slack in seconds as a JSON number of ps; null without a value or for INF, the
# slack of a side with no constrained path.
proc rb::ckpt::ps {seconds} {
    if {![string is double -strict $seconds] || $seconds != $seconds \
            || abs($seconds) >= 1e20} {
        return null
    }
    return [format %.3f [expr {$seconds * 1e12}]]
}

# OpenSTA 3.0 names scenes what earlier versions call corners.
proc rb::ckpt::find_scene {name} {
    if {[info commands ::sta::find_scene] ne ""} {
        return [sta::find_scene $name]
    }
    return [sta::find_corner $name]
}

# Setup and hold worst slack, then setup and hold TNS, of corner `name`, in
# seconds. One query each; the session's one default scene without `scenes`.
proc rb::ckpt::corner_slacks {name} {
    variable scenes
    if {!$scenes} {
        return [list [sta::worst_slack_cmd max] [sta::worst_slack_cmd min] \
            [sta::total_negative_slack_cmd max] \
            [sta::total_negative_slack_cmd min]]
    }
    set scene [find_scene $name]
    if {[info commands ::sta::worst_slack_scene] ne ""} {
        return [list [sta::worst_slack_scene $scene max] \
            [sta::worst_slack_scene $scene min] \
            [sta::total_negative_slack_scene_cmd $scene max] \
            [sta::total_negative_slack_scene_cmd $scene min]]
    }
    return [list [sta::worst_slack_corner $scene max] \
        [sta::worst_slack_corner $scene min] \
        [sta::total_negative_slack_corner_cmd $scene max] \
        [sta::total_negative_slack_corner_cmd $scene min]]
}

# Append a `stage_timing` event for `stage` at `point`, on the parasitics now
# set, which `source` names. Every corner is listed; a side with no constrained
# path is null with its TNS, and a corner that cannot be queried, or all of
# them after an `error`, is null with the reason.
proc rb::ckpt::timing_emit {stage point source round error} {
    variable corners
    variable order
    set entries {}
    set unavailable ""
    foreach name $corners {
        set values {null null null null}
        set reason $error
        if {$reason eq ""} {
            if {[catch {corner_slacks $name} slacks]} {
                set reason $slacks
            } else {
                lassign $slacks setup hold tns tns_hold
                # A side with no constrained path has no TNS either.
                set values [list [ps $setup] [ps $hold] null null]
                if {[lindex $values 0] ne "null"} {
                    lset values 2 [ps $tns]
                }
                if {[lindex $values 1] ne "null"} {
                    lset values 3 [ps $tns_hold]
                }
            }
        }
        lassign $values setup hold tns tns_hold
        set body "\"wns_setup_ps\": $setup, \"wns_hold_ps\": $hold,"
        append body " \"tns_ps\": $tns, \"tns_hold_ps\": $tns_hold"
        if {$reason ne ""} {
            append body ", \"error\": [json_str $reason]"
            set unavailable $reason
        }
        lappend entries "[json_str $name]: \{$body\}"
    }
    if {$unavailable ne ""} {
        puts ">>> rb checkpoints: timing of $stage ($point) unavailable: $unavailable"
    }
    set fields [list stage [json_str $stage]]
    if {[dict exists $order $stage]} {
        lappend fields index [json_str [dict get $order $stage]]
    }
    lappend fields point [json_str $point]
    if {$round ne ""} {
        lappend fields round $round
    }
    lappend fields parasitics [json_str $source] \
        corners "\{[join $entries {, }]\}"
    emit stage_timing $fields
}

# Time `stage` at `point` on the parasitics now set, as the flow's own reports
# do. Timing is a side record: a failure is logged and never fails the flow.
proc rb::ckpt::timing_event {stage point source {round ""}} {
    if {[catch {timing_emit $stage $point $source $round ""} message]} {
        puts ">>> rb checkpoints: timing of $stage ($point) failed: $message"
    }
}

# Time `stage` at `point` on the parasitics its own report uses, which this
# sets first, once. A stage with no entry in `timed` is timed elsewhere.
proc rb::ckpt::stage_timing {stage point} {
    variable timed
    variable timed_done
    if {![dict exists $timed $stage] || "$stage/$point" in $timed_done} {
        return
    }
    lappend timed_done "$stage/$point"
    lassign [dict get $timed $stage] source command
    set error ""
    if {$command ne "" && [catch {uplevel #0 $command} message]} {
        set error $message
    }
    if {[catch {timing_emit $stage $point $source "" $error} message]} {
        puts ">>> rb checkpoints: timing of $stage ($point) failed: $message"
    }
}

# Save the globals named in `state` as Tcl a later session sources.
proc rb::ckpt::write_state {path} {
    variable state
    set fh [open $path w]
    foreach name $state {
        if {[info exists ::$name]} {
            puts $fh [list set $name [set ::$name]]
        }
    }
    close $fh
}

# One checkpoint: ODB (what a resume re-enters), DEF (what KLayout stream-out
# reads, so `rb pnr-export --checkpoint` needs no OpenROAD), the SDC and each
# of `modes`' own, for the stages in `routed` the route guides and global-route
# segments, in a staged run the saved globals, and a copy of each of the
# stage's `reports` that it wrote. Guides alone do not restore the parasitics
# `estimate_parasitics -global_routing` needs. The supply connections of the
# cells added since `pdngen` are made first.
#
# The `checkpoint` event is written after every file is on disk; a checkpoint
# without one is incomplete. With `strict` a failed write is an error.
proc rb::ckpt::write_checkpoint {index stage {strict 0}} {
    variable dir
    variable routed
    variable state
    variable reports
    global DESIGN OUT_DIR
    set base [file join $dir "${index}_${stage}"]
    variable modes
    set files [list odb "$base.odb" def "$base.def" sdc "$base.sdc"]
    foreach mode $modes {
        lappend files sdc_$mode "$base.$mode.sdc"
    }
    if {[lsearch -exact $routed $stage] >= 0} {
        lappend files guides "$base.guide" segments "$base.segments"
    }
    if {[llength $state] > 0} {
        lappend files state "$base.state.tcl"
    }
    set copies {}
    if {[dict exists $reports $stage]} {
        foreach {kind name} [dict get $reports $stage] {
            set report [file join $OUT_DIR $name]
            if {[file exists $report]} {
                set copy "$base.[file tail $name]"
                lappend files $kind $copy
                dict set copies $copy $report
            }
        }
    }
    puts ">>> Checkpoint ${index}_${stage}"
    emit checkpoint_begin [list stage [json_str $stage] index [json_str $index]]
    if {[catch {
        rb::global_connect
        file mkdir $dir
        foreach {kind path} $files {
            switch -glob -- $kind {
                odb      { write_db $path }
                def      { write_def $path }
                sdc      { write_sdc -no_timestamp -digits 9 $path }
                sdc_*    { if {![file exists $path]} { error "$path not written" } }
                guides   { write_guides $path }
                segments { write_global_route_segments $path }
                state    { write_state $path }
                default  { file copy -force [dict get $copies $path] $path }
            }
        }
    } err]} {
        puts ">>> rb checkpoints: ${index}_${stage} not written: $err"
        emit checkpoint [list stage [json_str $stage] index [json_str $index] \
            status [json_str error] error [json_str $err]]
        if {$strict} {
            error "checkpoint ${index}_${stage} not written: $err"
        }
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

# Arm progress, stage resources and checkpoints. `steps` are the traced flow
# commands; `config` is a dict of the namespace variables above (anchors,
# wanted, order, routed, reports, state, account, strict, timed, corners,
# scenes). A session that
# measures its stages starts measuring the first one here.
proc rb::ckpt::arm {ckpt_dir progress_file steps config} {
    variable dir $ckpt_dir
    variable progress $progress_file
    foreach key {anchors wanted order routed reports state account strict timed corners scenes modes} {
        if {[dict exists $config $key]} {
            variable $key [dict get $config $key]
        }
    }
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
    foreach stage $wanted { lappend staged [json_str $stage] }
    emit flow_begin [list steps "\[[join $traced {, }]\]" \
        checkpoints "\[[join $staged {, }]\]"]
    begin_stage [lindex [dict keys $order] 0]
}
