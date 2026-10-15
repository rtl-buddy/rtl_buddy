# Macro pin-limit repair for the `rb pnr` OpenROAD flow (rtl_buddy#838).
#
# Substituted into pnr.tcl only when the platform sets
# `placement.macro-pin-limits`, and called after the post-placement
# `repair_design` and the legalisation that follows it, before CTS.
# `repair_design` sizes its repeaters for the library-wide target slew, so a
# macro input whose Liberty `max_transition` sits far below that target
# (OpenRAM SKY130 SRAMs: 0.04 ns) stays over its limit after every repair pass.
# This step buffers each such pin with a strong cell fixed next to it.
#
# It reads the checks on the legal placement: legalisation can move a macro
# output's loads tens of microns, so checks read on the unlegalised placement
# miss output pins that end up over their capacitance limit.
#
# The helpers before `repair` take and return plain numbers and strings, so
# tests/test_pnr_macro_pin_limits.py runs them under a bare Tcl interpreter.
# Geometry is in integral database units.

namespace eval rb::macro_pins {}

# The checks of one `report_check_types -max_slew -max_capacitance` report, as
# a dict: `slew` and `capacitance`, each a list of {pin limit value}. Values
# are in the report's units, which are the load library's thresholds.
proc rb::macro_pins::parse_checks {text} {
    set checks [dict create slew {} capacitance {}]
    set kind ""
    set number {-?[0-9]+(?:\.[0-9]*)?(?:[eE][-+]?[0-9]+)?}
    foreach line [split $text "\n"] {
        if {[regexp {^\s*max slew\s*$} $line]} {
            set kind slew
            continue
        }
        if {[regexp {^\s*max capacitance\s*$} $line]} {
            set kind capacitance
            continue
        }
        if {[regexp {^\s*max fanout\s*$} $line]} {
            set kind ""
            continue
        }
        if {$kind eq ""} {
            continue
        }
        # Pin, an optional scene name, limit, value, slack, verdict.
        set row "^(\\S+)(?:\\s+\[A-Za-z_\]\\S*)?\\s+($number)\\s+($number)\\s+($number)\\s+\\((?:VIOLATED|MET)\\)"
        if {[regexp $row $line -> pin limit value slack]} {
            dict lappend checks $kind [list $pin $limit $value]
        }
    }
    return $checks
}

# The worst {limit value} of `pin` in `checks` for `kind`, by the smallest
# headroom, or an empty list when the report has no such check.
proc rb::macro_pins::pin_check {checks kind pin} {
    set worst {}
    foreach row [dict get $checks $kind] {
        lassign $row name limit value
        if {$name ne $pin} {
            continue
        }
        if {$worst eq "" || $limit - $value < [lindex $worst 0] - [lindex $worst 1]} {
            set worst [list $limit $value]
        }
    }
    return $worst
}

# Whether `value` is over `limit` less a `margin` fraction of it.
proc rb::macro_pins::over {value limit margin} {
    return [expr {$value > $limit * (1.0 - $margin)}]
}

# Whether the box {x0 y0 x1 y1} overlaps none of `boxes` (touching is not
# overlapping).
proc rb::macro_pins::free {boxes x0 y0 x1 y1} {
    foreach box $boxes {
        lassign $box bx0 by0 bx1 by1
        if {$x0 < $bx1 && $bx0 < $x1 && $y0 < $by1 && $by0 < $y1} {
            return 0
        }
    }
    return 1
}

# The free row site nearest `target` ({x y}) for a cell `width` wide, as
# {x y orient}, or an empty list when no row has one.
#
# `rows` is a list of {x0 y0 x1 y1 pitch orient}: a row's box, its site pitch
# and orientation. `blockers` is a list of boxes the cell may not overlap. The
# cell keeps `pad` free on both sides, inside its row and clear of every
# blocker, so a blocker that is itself a padded cell must be passed grown by
# its own padding. Distance is Manhattan, from the target to the cell's centre.
#
# The space before each closing brace of an argument list keeps two adjacent
# closing braces, the flow template's placeholder delimiter, out of pnr.tcl.
proc rb::macro_pins::nearest_site {rows blockers target width pad } {
    lassign $target tx ty
    set order {}
    foreach row $rows {
        lassign $row x0 y0 x1 y1
        lappend order [list [expr {abs(($y0 + $y1) / 2.0 - $ty)}] $row]
    }
    set best {}
    set best_d ""
    foreach entry [lsort -real -index 0 $order] {
        lassign $entry dy row
        if {$best_d ne "" && $dy >= $best_d} {
            break
        }
        lassign $row x0 y0 x1 y1 pitch orient
        set row_blockers {}
        foreach box $blockers {
            lassign $box bx0 by0 bx1 by1
            if {$by0 < $y1 && $y0 < $by1} {
                lappend row_blockers $box
            }
        }
        # Sites k with the padded cell inside the row.
        set first [expr {int(ceil(double($pad) / $pitch))}]
        set last [expr {int(floor(double($x1 - $x0 - $width - $pad) / $pitch))}]
        if {$last < $first} {
            continue
        }
        set k0 [expr {int(round(($tx - $x0 - $width / 2.0) / $pitch))}]
        if {$k0 < $first} {
            set k0 $first
        } elseif {$k0 > $last} {
            set k0 $last
        }
        for {set step 0} {1} {incr step} {
            if {$best_d ne "" && ($step - 1) * $pitch + $dy >= $best_d} {
                break
            }
            set ks [expr {$step == 0 ? [list $k0] : [list [expr {$k0 - $step}] [expr {$k0 + $step}]]}]
            set inside 0
            foreach k $ks {
                if {$k < $first || $k > $last} {
                    continue
                }
                set inside 1
                set x [expr {$x0 + $k * $pitch}]
                set d [expr {abs($x + $width / 2.0 - $tx) + $dy}]
                if {$best_d ne "" && $d >= $best_d} {
                    continue
                }
                if {[free $row_blockers [expr {$x - $pad}] $y0 [expr {$x + $width + $pad}] $y1]} {
                    set best [list $x $y0 $orient]
                    set best_d $d
                }
            }
            if {!$inside} {
                break
            }
        }
    }
    return $best
}

# The x of a cell `width` wide inside a fixed slot from `x0`, `slot_width`
# wide, on the site nearest `px`. The slot was reserved for the widest
# candidate, so a narrower one slides toward the pin without leaving it.
proc rb::macro_pins::slot_x {x0 slot_width width px pitch} {
    set last [expr {($slot_width - $width) / $pitch}]
    set k [expr {int(round(($px - $width / 2.0 - $x0) / $pitch))}]
    if {$k < 0} {
        set k 0
    } elseif {$k > $last} {
        set k $last
    }
    return [expr {$x0 + $k * $pitch}]
}

# ---------------------------------------------------------------------------
# OpenROAD side.

# The parsed max-slew and max-capacitance checks of every pin on `net`.
proc rb::macro_pins::net_checks {net} {
    sta::redirect_string_begin
    set failed [catch {
        report_check_types -max_slew -max_capacitance -net $net -max_count 100000 -digits 6
    } err]
    set text [sta::redirect_string_end]
    if {$failed} {
        error $err
    }
    return [parse_checks $text]
}

# The macro pin's checks against its own limits: {slew_limit slew cap_limit
# cap}, an empty string for a check the report does not carry.
proc rb::macro_pins::measure {pin} {
    set checks [net_checks [get_nets -of_objects $pin]]
    set name [get_full_name $pin]
    set result {}
    foreach kind {slew capacitance} {
        set check [pin_check $checks $kind $name]
        if {[llength $check] == 2} {
            lappend result {*}$check
        } else {
            lappend result "" ""
        }
    }
    return $result
}

# Whether a measurement (from `measure`) meets its limits less the margins
# ({slew capacitance}, each a fraction of its limit).
# `kinds` names the checks that count: inputs are judged on slew, outputs on
# slew and capacitance.
proc rb::macro_pins::fits {measured kinds margins} {
    lassign $measured slew_limit slew cap_limit cap
    lassign $margins slew_margin cap_margin
    if {"slew" in $kinds && $slew ne "" && [over $slew $slew_limit $slew_margin]} {
        return 0
    }
    if {"capacitance" in $kinds && $cap ne "" && [over $cap $cap_limit $cap_margin]} {
        return 0
    }
    return 1
}

# Re-estimate the placement parasitics of every net on `insts`, whose pins
# moved with a master swap. A full `estimate_parasitics` per trial would cost
# a design-wide estimate each time.
proc rb::macro_pins::reestimate {insts} {
    foreach inst $insts {
        if {$inst eq ""} {
            continue
        }
        foreach net [get_nets -quiet -of_objects $inst] {
            est::estimate_parasitic_net $net
        }
    }
}

proc rb::macro_pins::db_pin {inst io} {
    foreach iterm [$inst getITerms] {
        set mterm [$iterm getMTerm]
        if {[$mterm getSigType] eq "SIGNAL" && [$mterm getIoType] eq $io} {
            return [sta::find_pin "[$inst getName]/[$mterm getName]"]
        }
    }
    error "rb macro-pin-limits: [[$inst getMaster] getName] has no $io signal pin"
}

# The report's unit for `quantity` (`time`, `capacitance`), e.g. `ns`.
proc rb::macro_pins::unit {quantity} {
    if {[catch {sta::unit_scale_suffix $quantity} suffix]} {
        return ""
    }
    regsub {^[0-9.]+} $suffix {} suffix
    return $suffix
}

proc rb::macro_pins::describe {measured kinds} {
    lassign $measured slew_limit slew cap_limit cap
    set parts {}
    if {"slew" in $kinds && $slew ne ""} {
        lappend parts [format "slew %.4f %s (limit %.4f)" $slew [unit time] $slew_limit]
    }
    if {"capacitance" in $kinds && $cap ne ""} {
        lappend parts [format "cap %.4f %s (limit %.4f)" $cap [unit capacitance] $cap_limit]
    }
    return [join $parts ", "]
}

# Buffer every macro data pin whose max-slew or max-capacitance check is over
# its limit less a margin: `margin` of a max-slew limit, `cap_margin` of a
# max-capacitance one. Placement parasitics underestimate a long routed net's
# capacitance more than a buffered pin's slew, hence the two.
#
# `buffers` are the candidate buffer cells, weakest first. `pad_sites` is the
# placement padding in sites on each side of a cell, as global placement uses
# it. `tag` prefixes the count line Python parses.
proc rb::macro_pins::repair {buffers margin cap_margin pad_sites tag} {
    set margins [list $margin $cap_margin]
    set db [ord::get_db]
    set block [ord::get_db_block]

    # Insert at the widest candidate, so the site each buffer is fixed to holds
    # whichever candidate the sizing settles on.
    set widest ""
    foreach name $buffers {
        set master [$db findMaster $name]
        if {$master eq "NULL"} {
            error "rb macro-pin-limits: buffer cell $name (placement.macro-pin-limits.buffers) is in no LEF"
        }
        if {$widest eq "" || [$master getWidth] > [$widest getWidth]} {
            set widest $master
        }
    }
    set widest_name [$widest getName]
    set width [$widest getWidth]

    set rows {}
    set pitch ""
    foreach row [$block getRows] {
        set box [$row getBBox]
        lappend rows [list [$box xMin] [$box yMin] [$box xMax] [$box yMax] \
            [$row getSpacing] [$row getOrient]]
        if {$pitch eq ""} {
            set pitch [$row getSpacing]
        }
    }
    if {$pitch eq ""} {
        error "rb macro-pin-limits: the floorplan has no rows"
    }
    set pad [expr {$pad_sites * $pitch}]

    # Hard placement blockages (each macro's cell halo among them) and fixed
    # instances. A fixed standard cell carries the placement padding as well.
    set blockers {}
    foreach blockage [$block getBlockages] {
        if {[$blockage isSoft] || [$blockage getMaxDensity] > 0} {
            continue
        }
        set box [$blockage getBBox]
        lappend blockers [list [$box xMin] [$box yMin] [$box xMax] [$box yMax]]
    }
    set macros {}
    foreach inst [$block getInsts] {
        if {![$inst isFixed]} {
            continue
        }
        set box [$inst getBBox]
        if {[[$inst getMaster] isBlock]} {
            lappend macros $inst
            lappend blockers [list [$box xMin] [$box yMin] [$box xMax] [$box yMax]]
        } else {
            lappend blockers [list [expr {[$box xMin] - $pad}] [$box yMin] \
                [expr {[$box xMax] + $pad}] [$box yMax]]
        }
    }

    # Every check is read on the legal placement, before the netlist changes.
    estimate_parasitics -placement
    set todo {}
    foreach inst $macros {
        foreach iterm [$inst getITerms] {
            set mterm [$iterm getMTerm]
            set io [$mterm getIoType]
            if {[$mterm getSigType] ne "SIGNAL" || ($io ne "INPUT" && $io ne "OUTPUT")} {
                continue
            }
            set net [$iterm getNet]
            if {$net eq "NULL" || [$net isSpecial] || [$net getSigType] ne "SIGNAL"} {
                continue
            }
            set pin [sta::find_pin "[$inst getName]/[$mterm getName]"]
            if {$pin eq "NULL" || $pin eq "" || [sta::is_clock $pin]} {
                continue
            }
            set kinds slew
            if {$io eq "OUTPUT"} {
                lappend kinds capacitance
            }
            set before [measure $pin]
            if {![fits $before $kinds $margins]} {
                lappend todo [list $pin $iterm $io $kinds $before]
            }
        }
    }

    set buffered {}
    set unmet 0
    foreach item $todo {
        lassign $item pin iterm io kinds before
        set name [get_full_name $pin]
        lassign [$iterm getAvgXY] ok px py
        set site [nearest_site $rows $blockers [list $px $py] $width $pad]
        if {[llength $site] != 3} {
            puts ">>>   $name: no free row site for $widest_name; left unbuffered"
            incr unmet
            continue
        }
        lassign $site sx sy orient
        if {$io eq "INPUT"} {
            set final [insert_buffer -load_pins [list $pin] -buffer_cell $widest_name \
                -buffer_name rb_macro_pin_buf -net_name rb_macro_pin_net]
        } else {
            set final [insert_buffer -net [get_nets -of_objects $pin] -buffer_cell $widest_name \
                -buffer_name rb_macro_pin_buf -net_name rb_macro_pin_net]
        }
        set final_db [sta::sta_to_db_inst $final]
        $final_db setOrient $orient
        $final_db setLocation $sx $sy
        $final_db setPlacementStatus FIRM
        lappend blockers [list [expr {$sx - $pad}] $sy [expr {$sx + $width + $pad}] \
            [expr {$sy + [$widest getHeight]}]]
        set nets [list [get_nets -of_objects $pin]]
        set pre ""
        if {$io eq "INPUT"} {
            # A pre-driver in front of the final buffer, so the strong buffer's
            # input load does not land on the original net. Not fixed: the
            # legaliser places it next to the final buffer.
            set final_in [db_pin $final_db INPUT]
            set pre [insert_buffer -load_pins [list $final_in] -buffer_cell $widest_name \
                -buffer_name rb_macro_pin_pre -net_name rb_macro_pin_net]
            set pre_db [sta::sta_to_db_inst $pre]
            $pre_db setOrient $orient
            $pre_db setLocation $sx $sy
            $pre_db setPlacementStatus PLACED
            lappend nets [get_nets -of_objects $final_in]
        }
        lappend buffered [list $pin $io $kinds $before $final $pre $nets [list $sx $sy $px]]
    }

    # Supplies first: `global_connect` skips dont_touch instances (ODB-0383).
    global_connect
    detailed_placement
    estimate_parasitics -placement

    # Weakest first: the final buffer, then for each final buffer the weakest
    # pre-driver that makes the pin fit. With none fitting, the last pair
    # (the strongest) stays.
    set dont_touch {}
    set results {}
    foreach item $buffered {
        lassign $item pin io kinds before final pre nets slot
        lassign $slot sx sy px
        set final_db [sta::sta_to_db_inst $final]
        set met 0
        foreach final_cell $buffers {
            replace_cell $final $final_cell
            # ODB refuses to move a FIRM instance.
            $final_db setPlacementStatus PLACED
            $final_db setLocation [slot_x $sx $width [[$final_db getMaster] getWidth] $px $pitch] $sy
            $final_db setPlacementStatus FIRM
            set pre_cells [expr {$pre eq "" ? [list ""] : $buffers}]
            foreach pre_cell $pre_cells {
                if {$pre ne ""} {
                    replace_cell $pre $pre_cell
                }
                reestimate [list $final $pre]
                set after [measure $pin]
                if {[fits $after $kinds $margins]} {
                    set met 1
                    break
                }
            }
            if {$met} {
                break
            }
        }
        if {!$met} {
            incr unmet
        }
        lappend dont_touch $final {*}$nets
        if {$pre ne ""} {
            lappend dont_touch $pre
        }
        lappend results [list $pin $io $kinds $before $after $final_cell $pre_cell $met]
    }

    # Sizing swapped masters; connect the supplies again before dont_touch.
    global_connect
    if {[llength $dont_touch] > 0} {
        set_dont_touch $dont_touch
    }

    foreach result $results {
        lassign $result pin io kinds before after final_cell pre_cell met
        set cells [expr {$pre_cell eq "" ? "buffer $final_cell" : "pre-driver $pre_cell, final $final_cell"}]
        set verdict [expr {$met ? "" : "; over the limit less the margin with the strongest candidate"}]
        puts ">>>   [get_full_name $pin] ([string tolower $io]): [describe $before $kinds] -> [describe $after $kinds]; $cells$verdict"
    }
    puts "$tag buffered [llength $results] unmet $unmet"
}
