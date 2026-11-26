"""How `rb power` reports what OpenSTA did with a trace's activity.

`read_saif` / `read_power_activities` store each annotated pin in OpenSTA's user activity map, and
`report_activity_annotation` counts that map. `report_power` uses a different map: the effective
activities `Power::ensureActivities` derives by seeding from the annotation and propagating. A pin
can be annotated and still not take its annotated value:

- a pin of the clock network takes the SDC clock's rate (origin ``clock``);
- a pin the activity search never visits keeps no activity at all (origin ``unknown``, density 0).
  The search does not cross a register clock-to-output arc, and only a cell with an ``ff`` or
  ``latch`` group gets its outputs seeded, so a macro whose Liberty has a clock-to-output arc but no
  such group leaves its outputs, and the pins they drive, unvisited.

This module generates the Tcl that records both maps and audits one against the other:

- :func:`annotation_tcl` writes `report_activity_annotation` to a file: the counts, and with
  ``list_pins`` the shorter of the annotated and the unannotated pin listings;
- :func:`census_tcl` walks every pin once and writes a per-origin count of the effective
  activities, plus one line per pin whose effective activity came neither from the trace nor from
  propagation;
- :func:`audit_activity` joins the two: an annotated pin whose effective origin is ``clock`` is
  counted in ``clock`` (rtl-buddy/rtl_buddy#1048 is about whether that value is right), any other
  non-trace origin in ``unused``.

Both Tcl blocks are by-products: they sit inside a ``catch`` and write under staging names renamed
on success, like the per-instance block, so a missing command in an older OpenSTA costs only the
report.
"""

from __future__ import annotations

import heapq
import re
from dataclasses import dataclass, field
from pathlib import Path

#: The effective-activity origin OpenSTA gives a pin whose activity came from each trace source.
TRACE_ORIGINS = {"saif": "saif", "vcd": "vcd"}

#: First line of the census file; the parser refuses any other.
CENSUS_HEADER = "# rtl_buddy activity census v1"

#: Pin names quoted in a result and in the warning; the counts cover every pin.
EXAMPLE_PINS = 5

# `report_activity_annotation` count rows, e.g. `saif           16` / `unannotated     0`.
_COUNT_RE = re.compile(r"^\s*([A-Za-z_]+)\s+(\d+)\s*$")
_ANNOTATED_HEADER = "Annotated pins:"
_UNANNOTATED_HEADER = "Unannotated pins:"


def _tcl_word(path: str) -> str:
    """Brace-quote ``path`` for Tcl so a space or ``$`` in it is taken literally."""
    return "{" + path + "}"


def annotation_tcl(path: str, staging: str, *, list_pins: bool) -> list[str]:
    """Tcl that writes `report_activity_annotation` to ``path``, via ``staging``.

    Without ``list_pins`` the file holds the counts only. With it, the counts are written first and
    read back, and the report is rewritten with whichever listing is shorter: ``-report_unannotated``
    when fewer pins are unannotated than annotated (a gate-level trace dumped to cell-pin depth),
    else ``-report_annotated``. :func:`audit_activity` reads either. Sorting and printing the
    listing costs several microseconds a pin, so the shorter one keeps a well-annotated design's
    audit cheap.
    """
    out = _tcl_word(staging)
    lines = [
        "catch {",
        f"  report_activity_annotation > {out}",
    ]
    if list_pins:
        lines += [
            f"  set rb_fh [open {out} r]",
            "  set rb_text [read $rb_fh]",
            "  close $rb_fh",
            "  set rb_ann 0",
            "  set rb_unann -1",
            "  foreach {rb_m rb_k rb_n} [regexp -all -inline -line "
            "{^\\s*([A-Za-z_]+)\\s+([0-9]+)\\s*$} $rb_text] {",
            '    if {$rb_k eq "unannotated"} {set rb_unann $rb_n} '
            "else {incr rb_ann $rb_n}",
            "  }",
            "  if {$rb_unann >= 0 && $rb_unann < $rb_ann} {",
            f"    report_activity_annotation -report_unannotated > {out}",
            "  } else {",
            f"    report_activity_annotation -report_annotated > {out}",
            "  }",
        ]
    lines += [
        f"  file rename -force {out} {_tcl_word(path)}",
        "}",
        f"catch {{file delete -force {out}}}",
    ]
    return lines


def census_tcl(path: str, staging: str, trace_origin: str) -> list[str]:
    """Tcl that writes the census of effective pin activities to ``path``, via ``staging``.

    One pass over the pins `report_activity_annotation` counts: every pin of the top instance and
    every non-power, non-internal pin of each leaf instance. Each pin costs one ``get_property
    <pin> activity``, which returns ``{density duty origin}`` from the activities `report_power`
    used. A pin is written out only when its origin is neither ``trace_origin`` nor
    ``propagated``; the per-origin counts come last.

    The power/internal filter runs only on ``unknown`` pins, the only origin such a pin can take,
    so a well-annotated design pays nothing for it.
    """
    out = _tcl_word(staging)
    return [
        "catch {",
        f"  set rb_fh [open {out} w]",
        f'  puts $rb_fh "{CENSUS_HEADER}"',
        "  array unset rb_origin",
        "  foreach rb_inst [concat [list [sta::top_instance]] "
        "[sta::network_leaf_instances]] {",
        "    set rb_leaf [$rb_inst is_leaf]",
        "    set rb_it [$rb_inst pin_iterator]",
        "    while {[$rb_it has_next]} {",
        "      set rb_pin [$rb_it next]",
        "      set rb_act [get_property $rb_pin activity]",
        "      set rb_org [lindex $rb_act 2]",
        '      if {$rb_leaf && $rb_org eq "unknown" && ([$rb_pin is_pwr_gnd] || '
        "[lsearch -exact {internal power ground} [sta::pin_direction $rb_pin]] >= 0)} "
        "continue",
        "      incr rb_origin($rb_org)",
        f'      if {{$rb_org ne "{trace_origin}" && $rb_org ne "propagated"}} {{',
        '        puts $rb_fh "pin $rb_org [lindex $rb_act 0] [lindex $rb_act 1] '
        '[get_full_name $rb_pin]"',
        "      }",
        "    }",
        "    $rb_it finish",
        "  }",
        "  foreach {rb_o rb_n} [array get rb_origin] {",
        '    puts $rb_fh "origin $rb_o $rb_n"',
        "  }",
        "  close $rb_fh",
        f"  file rename -force {out} {_tcl_word(path)}",
        "}",
        "catch {close $rb_fh}",
        f"catch {{file delete -force {out}}}",
    ]


def parse_annotation_counts(text: str) -> dict[str, int] | None:
    """Return the count rows of a `report_activity_annotation` output, e.g. ``{"saif": 16, "unannotated": 0}``.

    Reads up to the pin listing. ``None`` when there is no ``unannotated`` row, which every
    OpenSTA that has the command prints.
    """
    counts: dict[str, int] = {}
    for line in text.splitlines():
        if line.strip() in (_ANNOTATED_HEADER, _UNANNOTATED_HEADER):
            break
        m = _COUNT_RE.match(line)
        if m:
            counts[m.group(1)] = int(m.group(2))
    return counts if "unannotated" in counts else None


@dataclass
class Census:
    """A parsed census file."""

    #: Effective origin -> pin count, over the pins `report_activity_annotation` counts.
    origins: dict[str, int] = field(default_factory=dict)
    #: Pin -> effective origin, for the pins whose activity is neither the trace's nor propagated.
    #: The density and duty OpenSTA used stay in the file, so a large census costs one string per pin.
    pins: dict[str, str] = field(default_factory=dict)


def parse_census(path: str) -> Census | None:
    """Parse a file :func:`census_tcl` wrote; ``None`` if it is absent or not one.

    A pin line is ``pin <origin> <density> <duty> <name>``, the name taking the rest of the line; an
    origin line is ``origin <origin> <count>``. An empty origin (an OpenSTA whose ``activity``
    property has no origin field) makes the census unusable, so it returns ``None``.
    """
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    census = Census()
    with fh:
        if fh.readline().rstrip("\n") != CENSUS_HEADER:
            return None
        for line in fh:
            parts = line.rstrip("\n").split(" ", 4)
            if parts[0] == "pin" and len(parts) == 5 and parts[1] and parts[4]:
                try:
                    float(parts[2]), float(parts[3])
                except ValueError:
                    return None
                census.pins[parts[4]] = parts[1]
            elif parts[0] == "origin" and len(parts) == 3 and parts[1]:
                try:
                    census.origins[parts[1]] = int(parts[2])
                except ValueError:
                    return None
            elif parts[0] in ("pin", "origin"):
                return None
    return census if census.origins else None


def audit_activity(
    annotation_path: str,
    census_path: str | None,
    trace_origin: str,
    *,
    examples: int = EXAMPLE_PINS,
) -> dict:
    """Join the annotation report and the census into the run's result fields.

    :returns: a dict with any of

        - ``activity_annotation``: the `report_activity_annotation` counts;
        - ``activity_origins``: effective origin -> pin count;
        - ``activity_unused``: ``{"count", "annotated", "by_origin", "examples"}`` for the
          trace-annotated pins whose effective origin is neither the trace nor ``clock``;
        - ``activity_annotated_clock``: ``{"count", "examples"}`` for the trace-annotated pins that
          take the SDC clock's activity instead.

      A key is absent when its input is: no annotation report, or no census (opted out, or an
      OpenSTA without the commands it uses). ``examples`` are the first pins by name.
    """
    fields: dict = {}
    try:
        text_head = _read_head(annotation_path)
    except OSError:
        return fields
    counts = parse_annotation_counts(text_head)
    if counts is not None:
        fields["activity_annotation"] = counts
    census = parse_census(census_path) if census_path else None
    if census is None:
        return fields
    fields["activity_origins"] = dict(sorted(census.origins.items()))
    annotated_fallback = _annotated_fallback_pins(annotation_path, census, trace_origin)
    annotated = (counts or {}).get(trace_origin, 0)
    if annotated_fallback is None or not annotated:
        # No listing (an OpenSTA without the flags) or nothing annotated: nothing to join.
        return fields
    clock = {pin for pin, origin in annotated_fallback.items() if origin == "clock"}
    unused = {
        pin: origin for pin, origin in annotated_fallback.items() if origin != "clock"
    }
    by_origin: dict[str, int] = {}
    for origin in unused.values():
        by_origin[origin] = by_origin.get(origin, 0) + 1
    fields["activity_unused"] = {
        "count": len(unused),
        "annotated": annotated,
        "by_origin": dict(sorted(by_origin.items())),
        "examples": heapq.nsmallest(examples, unused),
    }
    fields["activity_annotated_clock"] = {
        "count": len(clock),
        "examples": heapq.nsmallest(examples, clock),
    }
    return fields


def _annotated_fallback_pins(
    annotation_path: str, census: Census, trace_origin: str
) -> dict[str, str] | None:
    """The census pins (effective origin neither the trace nor propagated) the trace annotated.

    One streaming pass over the report's listing, which on a large design is one line per pin.
    From an annotated listing: the census pins it names with ``trace_origin``. From an unannotated
    listing: the census pins it does not name. ``None`` when the report has no listing.
    """
    kind = None
    found: dict[str, str] = {}
    with open(annotation_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped == _ANNOTATED_HEADER:
                kind = "annotated"
            elif stripped == _UNANNOTATED_HEADER:
                kind = "unannotated"
                found = dict(census.pins)
            elif kind == "annotated":
                parts = stripped.split(None, 1)
                if len(parts) == 2 and parts[0] == trace_origin:
                    origin = census.pins.get(parts[1])
                    if origin is not None:
                        found[parts[1]] = origin
            elif kind == "unannotated" and stripped:
                found.pop(stripped, None)
    return found if kind is not None else None


def _read_head(path: str, limit: int = 64 * 1024) -> str:
    """The start of ``path``: the count rows precede a listing that can be one line per pin."""
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        return fh.read(limit)
