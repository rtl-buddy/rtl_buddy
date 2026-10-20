
# An `eco` post-route repair round's repairs (see `_post_route_repair_script`).
# On a routed database `repair_timing` times on detailed-routing parasitics and
# builds buffer trees from the global routes a fresh session rebuilds from the
# database's guides, which can miss pins (RSZ-0074). `rb::eco::repair` runs the
# repairs with `design_is_routed` answering 0, so the resizer keeps the
# placement source: the SPEF read over it times the nets the repairs leave
# alone, and the nets they change are timed and buffered on Steiner trees.
# When `repair_timing` no longer asks `design_is_routed`, it runs them as they
# are and records a `post_route_eco_unguarded` progress event.
namespace eval rb::eco {}

proc rb::eco::overridable {} {
    if {![llength [info procs ::repair_timing]]
        || ![llength [info commands ::design_is_routed]]} {
        return 0
    }
    return [string match *design_is_routed* [info body ::repair_timing]]
}

proc rb::eco::repair {round script} {
    if {![overridable]} {
        puts ">>> Post-route repair round $round: repair_timing does not call\
            design_is_routed; repairing on the routed parasitics source"
        if {[llength [info commands ::rb::ckpt::emit]]} {
            ::rb::ckpt::emit post_route_eco_unguarded [list round $round]
        }
        return [uplevel #0 $script]
    }
    rename ::design_is_routed ::rb::eco::design_is_routed
    proc ::design_is_routed {args} { return 0 }
    set code [catch {uplevel #0 $script} result options]
    rename ::design_is_routed {}
    rename ::rb::eco::design_is_routed ::design_is_routed
    if {$code == 0} { return $result }
    return -options [dict incr options -level] $result
}
