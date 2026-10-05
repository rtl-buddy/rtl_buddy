# Power hookup of hardened `blocks:` instances, inlined by rb pnr around `pdngen`.
#
#   rb::block_power::tie_supplies MASTERS   after the pdn-config, before pdngen
#   rb::block_power::join_straps  MASTERS   after pdngen
#
# `tie_supplies` connects each power and ground pin of a block instance that the
# pdn-config's global connections left on no net: to the parent net of the
# pin's own name when it is a supply net of the same kind, else to the parent's
# only power (or ground) net.
#
# `join_straps` handles a block whose supply pins are on the parent's top strap
# layer. pdngen treats those pins as an obstruction and stops the parent's
# straps short of the block on both sides, so nothing reaches them. For each such
# pin it finds the nearest parent strap of the pin's net on the pin's track on
# either side, and adds one STRIPE box across the pin that overlaps each strap
# end by a strap width. The phase check: a pin with a shape on or above the top
# strap layer and none joined fails the run. That is a pin off its own net's
# straps (a misplaced or mirrored block), a join that would touch another net's
# strap (a rotated block), a pin above the top strap layer, or a parent grid
# with no straps at all. A stray shape of a pin that is otherwise joined is
# warned about with an `RB-BLOCK-POWER-WARNING:` line.
#
# Rectangles are {xlo ylo xhi yhi} in DBU. Problems are printed as
# `RB-BLOCK-POWER-ERROR: ...` lines, which rb reports, and the run stops.

namespace eval rb::block_power {}

# The parent net a pin of signal type `sig` ties to, or "" when there is no
# single answer. `nets` lists the parent's supply net names of that type.
proc rb::block_power::pick_net {pin nets} {
  if {[lsearch -exact $nets $pin] >= 0} {
    return $pin
  }
  if {[llength $nets] == 1} {
    return [lindex $nets 0]
  }
  return ""
}

# How to join a pin to the parent's straps on its layer.
#
# `own` are the straps of the pin's net on that layer and `others` those of the
# other supply nets, as {name rect} pairs. Returns one of
#   {covered}            a strap already overlaps the pin
#   {join RECT}          add RECT
#   {misaligned NAMES NEAREST}
#                        no own strap is on the pin's track. NAMES are the other
#                        nets whose straps are (empty when none is); NEAREST is
#                        the centre line of the closest own strap, or ""
#   {short NAMES}        the join would touch a strap of another net, as for a
#                        rotated block whose pin runs across the straps
proc rb::block_power::plan_join {pin horizontal own others} {
  # Along the strap: x on a horizontal layer. Across it: y.
  if {$horizontal} {
    lassign {0 1 2 3} a0 c0 a1 c1
  } else {
    lassign {1 0 3 2} a0 c0 a1 c1
  }
  set centre [expr {([lindex $pin $c0] + [lindex $pin $c1]) / 2}]
  set before ""
  set after ""
  foreach s $own {
    if {$centre < [lindex $s $c0] || $centre > [lindex $s $c1]} {
      continue
    }
    if {[lindex $s $a1] < [lindex $pin $a0]} {
      if {$before eq "" || [lindex $s $a1] > [lindex $before $a1]} {
        set before $s
      }
    } elseif {[lindex $s $a0] > [lindex $pin $a1]} {
      if {$after eq "" || [lindex $s $a0] < [lindex $after $a0]} {
        set after $s
      }
    } else {
      return [list covered]
    }
  }
  if {$before eq "" && $after eq ""} {
    set names {}
    foreach pair $others {
      lassign $pair name s
      if {$centre >= [lindex $s $c0] && $centre <= [lindex $s $c1]} {
        lappend names $name
      }
    }
    set nearest ""
    foreach s $own {
      set mid [expr {([lindex $s $c0] + [lindex $s $c1]) / 2}]
      if {$nearest eq "" || abs($mid - $centre) < abs($nearest - $centre)} {
        set nearest $mid
      }
    }
    return [list misaligned [lsort -unique $names] $nearest]
  }
  set box $pin
  if {$before ne ""} {
    set width [expr {[lindex $before $c1] - [lindex $before $c0]}]
    lset box $a0 [expr {max([lindex $before $a0], [lindex $before $a1] - $width)}]
  }
  if {$after ne ""} {
    set width [expr {[lindex $after $c1] - [lindex $after $c0]}]
    lset box $a1 [expr {min([lindex $after $a1], [lindex $after $a0] + $width)}]
  }
  set shorts {}
  foreach pair $others {
    lassign $pair name s
    if {[lindex $box 0] <= [lindex $s 2] && [lindex $s 0] <= [lindex $box 2]
        && [lindex $box 1] <= [lindex $s 3] && [lindex $s 1] <= [lindex $box 3]} {
      lappend shorts $name
    }
  }
  if {[llength $shorts]} {
    return [list short [lsort -unique $shorts]]
  }
  return [list join $box]
}

proc rb::block_power::instances {masters} {
  set out {}
  foreach inst [[ord::get_db_block] getInsts] {
    if {[lsearch -exact $masters [[$inst getMaster] getName]] >= 0} {
      lappend out $inst
    }
  }
  return $out
}

# The parent's special nets of one signal type, as a name -> net dict.
proc rb::block_power::supply_nets {sig} {
  set nets [dict create]
  foreach net [[ord::get_db_block] getNets] {
    if {[$net isSpecial] && [$net getSigType] eq $sig} {
      dict set nets [$net getName] $net
    }
  }
  return $nets
}

proc rb::block_power::regex_quote {text} {
  return [regsub -all {[][{}()*+?.\\^$|]} $text {\\&}]
}

proc rb::block_power::fail {errors} {
  foreach msg $errors {
    puts "RB-BLOCK-POWER-ERROR: $msg"
  }
  error "[llength $errors] block power problem(s); see the RB-BLOCK-POWER-ERROR lines"
}

proc rb::block_power::tie_supplies {masters} {
  set errors {}
  set tied 0
  foreach inst [instances $masters] {
    foreach iterm [$inst getITerms] {
      set mterm [$iterm getMTerm]
      set sig [$mterm getSigType]
      if {$sig ni {POWER GROUND} || [$iterm getNet] ne "NULL"} {
        continue
      }
      set pin [$mterm getName]
      set nets [lsort [dict keys [supply_nets $sig]]]
      set net_name [pick_net $pin $nets]
      if {$net_name eq ""} {
        set kind [string tolower $sig]
        set have [expr {[llength $nets] ? [join $nets ", "] : "none"}]
        lappend errors "[$inst getName]/$pin: no single parent $kind net to tie it to (parent $kind nets: $have); add an add_global_connection for it to the pdn-config"
        continue
      }
      set flag [expr {$sig eq "POWER" ? "-power" : "-ground"}]
      add_global_connection -net $net_name \
          -inst_pattern "^[regex_quote [$inst getName]]\$" \
          -pin_pattern "^[regex_quote $pin]\$" $flag
      puts "rb: tied [$inst getName]/$pin to $net_name"
      incr tied
    }
  }
  if {[llength $errors]} {
    fail $errors
  }
  if {$tied} {
    global_connect
  }
}

# pdngen's STRIPE shapes of `net` on `layer`.
proc rb::block_power::straps {net layer} {
  set out {}
  foreach swire [$net getSWires] {
    foreach box [$swire getWires] {
      if {[$box isVia] || [$box getWireShapeType] ne "STRIPE"} {
        continue
      }
      if {[[$box getTechLayer] getName] ne $layer} {
        continue
      }
      lappend out [list [$box xMin] [$box yMin] [$box xMax] [$box yMax]]
    }
  }
  return $out
}

# The highest routing layer that carries a supply strap, or "" with none.
proc rb::block_power::top_strap_layer {} {
  set top ""
  set top_level -1
  foreach sig {POWER GROUND} {
    foreach net [dict values [supply_nets $sig]] {
      foreach swire [$net getSWires] {
        foreach box [$swire getWires] {
          if {[$box isVia] || [$box getWireShapeType] ne "STRIPE"} {
            continue
          }
          set layer [$box getTechLayer]
          if {[$layer getRoutingLevel] > $top_level} {
            set top_level [$layer getRoutingLevel]
            set top [$layer getName]
          }
        }
      }
    }
  }
  return $top
}

proc rb::block_power::join_straps {masters} {
  set layer_name [top_strap_layer]
  set top_level -1
  if {$layer_name ne ""} {
    set layer [[ord::get_db_tech] findLayer $layer_name]
    set top_level [$layer getRoutingLevel]
    set horizontal [expr {[$layer getDirection] eq "HORIZONTAL"}]
    set axis [expr {$horizontal ? "y" : "x"}]
  }
  set strap_cache [dict create]
  if {$layer_name ne ""} {
    foreach sig {POWER GROUND} {
      dict for {name net} [supply_nets $sig] {
        dict set strap_cache $name [straps $net $layer_name]
      }
    }
  }
  set errors {}
  set warnings {}
  set joined_pins 0
  set joined_shapes 0
  foreach inst [instances $masters] {
    foreach iterm [$inst getITerms] {
      set mterm [$iterm getMTerm]
      if {[$mterm getSigType] ni {POWER GROUND}} {
        continue
      }
      set pin "[$inst getName]/[$mterm getName]"
      set net [$iterm getNet]
      # Shapes at or above the top strap layer are this proc's to connect; pdngen's macro grids handle the rest.
      set mine 0
      set reached 0
      set problems {}
      foreach geom [$iterm getGeometries] {
        lassign $geom pin_layer rect
        set level [$pin_layer getRoutingLevel]
        if {$level == 0 || $level < $top_level} {
          continue
        }
        incr mine
        if {$layer_name eq ""} {
          lappend problems "$pin is on [$pin_layer getName], and the parent's grid has no straps to join it to"
          break
        }
        if {$level > $top_level} {
          lappend problems "$pin has a shape on [$pin_layer getName], above the parent's top strap layer $layer_name, which nothing connects"
          continue
        }
        if {$net eq "NULL"} {
          lappend problems "$pin is on no net"
          break
        }
        set net_name [$net getName]
        set own {}
        if {[dict exists $strap_cache $net_name]} {
          set own [dict get $strap_cache $net_name]
        }
        set others {}
        dict for {name rects} $strap_cache {
          if {$name ne $net_name} {
            foreach r $rects {
              lappend others [list $name $r]
            }
          }
        }
        set p [list [$rect xMin] [$rect yMin] [$rect xMax] [$rect yMax]]
        set centre [expr {$horizontal \
            ? ([lindex $p 1] + [lindex $p 3]) / 2 \
            : ([lindex $p 0] + [lindex $p 2]) / 2}]
        set where "$net_name, $layer_name $axis=[format %.3f [ord::dbu_to_microns $centre]] um, [$inst getOrient]"
        set plan [plan_join $p $horizontal $own $others]
        switch -- [lindex $plan 0] {
          covered {
            incr reached
          }
          join {
            set swire [odb::dbSWire_create $net ROUTED]
            odb::dbSBox_create $swire $layer {*}[lindex $plan 1] STRIPE
            incr joined_shapes
            incr reached
          }
          short {
            lappend problems "$pin ($where) cannot be joined: the join would touch a [join [lindex $plan 1] /] strap"
          }
          misaligned {
            lassign $plan - names nearest
            set why [expr {[llength $names] \
                ? "it is on a [join $names /] strap's track" \
                : "no parent strap is on its track"}]
            if {$nearest ne ""} {
              append why ", the nearest $net_name strap is at $axis=[format %.3f [ord::dbu_to_microns $nearest]] um"
            }
            lappend problems "$pin ($where) is not on a parent $net_name strap: $why"
          }
        }
      }
      if {!$mine} {
        continue
      }
      if {$reached} {
        incr joined_pins
        foreach msg $problems {
          lappend warnings "$msg; the pin's other shapes are joined"
        }
      } else {
        foreach msg $problems {
          lappend errors "$msg; place and orient the block so its supply pins line up with the parent's [expr {$layer_name eq "" ? "" : "$layer_name "}]straps"
        }
      }
    }
  }
  foreach msg $warnings {
    puts "RB-BLOCK-POWER-WARNING: $msg"
  }
  if {[llength $errors]} {
    fail $errors
  }
  if {$joined_shapes} {
    puts "rb: joined $joined_pins block supply pin(s) on $layer_name to the parent's straps ($joined_shapes shape(s))"
  }
}
