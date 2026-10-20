# Per-macro placement directives for the `rb pnr` OpenROAD flow
# (`floorplan.macros`).
#
# Substituted into pnr.tcl at the start of the macro placement stage, only when
# the run sets `floorplan.macros`. `rb::macros::apply` orients the macros,
# records their cell halos, and fixes the ones with a location before the
# packer or RTL-MP runs. Both leave a fixed macro where it is; the packer also
# keeps the others a macro halo away from it.
#
# `snap` and `overlaps` are plain arithmetic, so tests/test_pnr_floorplan_macros.py
# runs them under a bare Tcl interpreter.

namespace eval rb::macros {}

# Snap `value` to the nearest multiple of `grid` counted from `origin`. A value
# whose box (`value` to `value + size`) lay inside `lo`..`hi` stays inside:
# rounding that would push it past an edge goes one step the other way.
proc rb::macros::snap {value grid origin lo hi size} {
    if {$grid <= 1} {
        return $value
    }
    set snapped [expr {$origin + int(round(double($value - $origin) / $grid)) * $grid}]
    if {$value + $size <= $hi && $snapped + $size > $hi} {
        set snapped [expr {$snapped - $grid}]
    }
    if {$value >= $lo && $snapped < $lo} {
        set snapped [expr {$snapped + $grid}]
    }
    return $snapped
}

# Whether boxes {x0 y0 x1 y1} overlap; touching edges do not.
proc rb::macros::overlaps {a b} {
    lassign $a ax0 ay0 ax1 ay1
    lassign $b bx0 by0 bx1 by1
    return [expr {$ax0 < $bx1 && $bx0 < $ax1 && $ay0 < $by1 && $by0 < $ay1}]
}

proc rb::macros::um {dbu dbu_per_micron} {
    return [format %.3f [expr {double($dbu) / $dbu_per_micron}]]
}

# The macro instances `pattern` names: the instance of that exact name, else
# every instance whose name matches it as a glob. An error names `where` when
# nothing matches or a standard cell does.
proc rb::macros::match {block where pattern} {
    set inst [$block findInst $pattern]
    if {$inst ne "NULL" && $inst ne ""} {
        set insts [list $inst]
    } else {
        set insts {}
        foreach inst [$block getInsts] {
            if {[string match $pattern [$inst getName]]} {
                lappend insts $inst
            }
        }
    }
    if {[llength $insts] == 0} {
        error "$where: no instance matches '$pattern' (floorplan.macros)"
    }
    foreach inst $insts {
        set master [$inst getMaster]
        if {![$master isBlock]} {
            error "$where: '$pattern' matches standard cell [$inst getName] ([$master getName]); floorplan.macros applies to hard macros only"
        }
    }
    return $insts
}

# Apply `entries`, each {where pattern location orientation halo} with "" for
# an unset key; `location` and `halo` are {x y} in microns. Returns the macros
# in `macros` still to be placed, those without a location.
#
# A macro matched by several entries takes each key from the one entry that
# sets it; two entries setting the same key on one macro is an error. A located
# macro is snapped to the site grid from the core's lower-left corner (`grid`
# is {site_width row_height} in DBU), must lie inside the core and clear of
# every other fixed macro, and is LOCKED. Its box grown by `halo_dbu` (the
# macro halo) is appended to the global MACRO_KEEPOUTS for the packer; its
# cell halo goes into the global MACRO_CELL_HALOS.
proc rb::macros::apply {block macros entries grid halo_dbu dbu_per_micron} {
    upvar #0 MACRO_CELL_HALOS cell_halos MACRO_KEEPOUTS keepouts
    if {![info exists keepouts]} {
        set keepouts {}
    }
    set core [$block getCoreArea]
    set core_box [list [$core xMin] [$core yMin] [$core xMax] [$core yMax]]
    lassign $core_box cx0 cy0 cx1 cy1
    lassign $grid grid_x grid_y
    set owner [dict create]
    set fixed {}
    foreach entry $entries {
        lassign $entry where pattern location orientation halo
        set insts [match $block $where $pattern]
        if {$location ne "" && [llength $insts] != 1} {
            error "$where: '$pattern' matches [llength $insts] macros; a location places exactly one (floorplan.macros)"
        }
        foreach inst $insts {
            set name [$inst getName]
            foreach key {location orientation halo} {
                if {[set $key] eq ""} {
                    continue
                }
                if {[dict exists $owner $name,$key]} {
                    error "$where: macro $name already has a $key from [dict get $owner $name,$key] (floorplan.macros)"
                }
                dict set owner $name,$key $where
            }
            if {$orientation ne ""} {
                $inst setOrient $orientation
            }
            if {$halo ne ""} {
                dict set cell_halos $name $halo
            }
        }
        if {$location eq ""} {
            continue
        }
        set inst [lindex $insts 0]
        set name [$inst getName]
        lassign $location x_um y_um
        set bbox [$inst getBBox]
        set width [$bbox getDX]
        set height [$bbox getDY]
        set x [snap [expr {int(round([ord::microns_to_dbu $x_um]))}] $grid_x $cx0 $cx0 $cx1 $width]
        set y [snap [expr {int(round([ord::microns_to_dbu $y_um]))}] $grid_y $cy0 $cy0 $cy1 $height]
        set box [list $x $y [expr {$x + $width}] [expr {$y + $height}]]
        if {$x < $cx0 || $y < $cy0 || $x + $width > $cx1 || $y + $height > $cy1} {
            error [format "%s: macro %s at (%s, %s) um, %s x %s um, does not fit inside the core (%s, %s)-(%s, %s) um (floorplan.macros)" \
                $where $name $x_um $y_um \
                [um $width $dbu_per_micron] [um $height $dbu_per_micron] \
                [um $cx0 $dbu_per_micron] [um $cy0 $dbu_per_micron] \
                [um $cx1 $dbu_per_micron] [um $cy1 $dbu_per_micron]]
        }
        foreach other $fixed {
            lassign $other other_name other_box
            if {[overlaps $box $other_box]} {
                error "$where: macro $name at ($x_um, $y_um) um overlaps fixed macro $other_name (floorplan.macros)"
            }
        }
        $inst setLocation $x $y
        $inst setPlacementStatus LOCKED
        lappend fixed [list $name $box]
        lappend keepouts [list \
            [expr {$x - $halo_dbu}] [expr {$y - $halo_dbu}] \
            [expr {$x + $width + $halo_dbu}] [expr {$y + $height + $halo_dbu}]]
        puts [format ">>>   %s: fixed %s at (%s, %s) um %s (requested (%s, %s))" \
            $where $name [um $x $dbu_per_micron] [um $y $dbu_per_micron] \
            [$inst getOrient] $x_um $y_um]
    }
    set movable {}
    foreach inst $macros {
        if {![$inst isFixed]} {
            lappend movable $inst
        }
    }
    return $movable
}
