# Core-grid replacement for a run's declarative `pdn:` block in the `rb pnr`
# OpenROAD flow.
#
# Substituted into pnr.tcl in place of the plain `source <pdn-config>`, only
# when the run sets `pdn:`. `rb::pdn::source_base` sources the pdn-config with
# its core grid left out: `define_pdn_grid` without `-macro` or `-existing`
# and the stripes, rings and connects of that grid are skipped and reported.
# Its global connections, voltage domains and macro grids run as written.
# `rb::pdn::define_core_grid` then defines the run's core grid with the
# skipped grid's name, voltage domains, pin layers and starting net.
#
# `option` and `grid_of` are plain list handling, so
# tests/test_pnr_floorplan_core.py runs them under a bare Tcl interpreter.

namespace eval rb::pdn {
    # The skipped core grids' names, and the define_pdn_grid arguments of the first.
    variable skipped {}
    variable core_args {}
    # Whether the most recently defined grid was skipped: a grid command
    # without -grid applies to it.
    variable current_skipped 0
}

# The value following `key` in an argument list, or "" when absent.
proc rb::pdn::option {arguments key} {
    set index [lsearch -exact $arguments $key]
    if {$index < 0 || $index + 1 >= [llength $arguments]} {
        return ""
    }
    return [lindex $arguments [expr {$index + 1}]]
}

# Whether a grid command (`add_pdn_stripe`, `add_pdn_ring`, `add_pdn_connect`)
# with these arguments targets a skipped core grid.
proc rb::pdn::targets_skipped {arguments skipped current_skipped} {
    set grid [option $arguments -grid]
    if {$grid eq ""} {
        return $current_skipped
    }
    return [expr {[lsearch -exact $skipped $grid] >= 0}]
}

proc rb::pdn::define_grid {args} {
    variable skipped
    variable core_args
    variable current_skipped
    if {"-macro" in $args || "-existing" in $args} {
        set current_skipped 0
        return [::rb::pdn::orig_define_pdn_grid {*}$args]
    }
    set name [option $args -name]
    lappend skipped $name
    if {[llength $skipped] == 1} {
        set core_args $args
    }
    set current_skipped 1
    puts ">>>   pdn: core grid '$name' from the pdn-config replaced by the run's pdn: block"
}

proc rb::pdn::grid_command {command args} {
    variable skipped
    variable current_skipped
    if {[targets_skipped $args $skipped $current_skipped]} {
        return
    }
    return [::rb::pdn::orig_$command {*}$args]
}

# Source the pdn-config at `path` without its core grid.
proc rb::pdn::source_base {path} {
    set commands {define_pdn_grid add_pdn_stripe add_pdn_ring add_pdn_connect}
    foreach command $commands {
        rename ::$command ::rb::pdn::orig_$command
    }
    proc ::define_pdn_grid {args} {
        return [::rb::pdn::define_grid {*}$args]
    }
    foreach command [lrange $commands 1 end] {
        proc ::$command {args} "return \[::rb::pdn::grid_command $command {*}\$args\]"
    }
    set code [catch {uplevel #0 [list source $path]} result options]
    foreach command $commands {
        rename ::$command {}
        rename ::rb::pdn::orig_$command ::$command
    }
    return -options $options $result
}

# Define the run's core grid and return its name: the skipped grid's name,
# voltage domains, pin layers and starting net, or `rb_core` with OpenROAD's
# defaults when the pdn-config defined no core grid.
proc rb::pdn::define_core_grid {} {
    variable skipped
    variable core_args
    set name [expr {[llength $skipped] > 0 && [lindex $skipped 0] ne "" ? [lindex $skipped 0] : "rb_core"}]
    set command [list define_pdn_grid -name $name]
    foreach key {-voltage_domains -pins -starts_with} {
        set value [option $core_args $key]
        if {$value ne ""} {
            lappend command $key $value
        }
    }
    puts ">>>   pdn: $command"
    {*}$command
    return $name
}
