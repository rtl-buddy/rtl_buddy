# The `stage-end` hook point: substituted after `hooks.tcl` only when pnr.yaml
# sets it. `rb::hook::stage_end` runs one file at the end of `stage` with the
# stage in `$rb_stage`; the flow sets `$rb_mode` before it. A failure names the
# stage.

proc rb::hook::stage_end {stage path} {
    set ::rb_stage $stage
    if {![catch {rb::hook::run stage-end $path} message options]} { return }
    set prefix "Stage $stage: "
    dict set options -errorinfo "$prefix[dict get $options -errorinfo]"
    return -options $options "$prefix$message"
}
