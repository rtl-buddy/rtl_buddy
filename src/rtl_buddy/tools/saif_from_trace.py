"""Convert an FST or VCD waveform trace to SAIF v2.0 (backward direction).

Uses pywellen to read the trace, walks the hierarchy, computes per-bit
T0/T1/TX/TZ time-in-state and TC toggle counters, and emits SAIF in the
trace's native timescale so values are exact integers (no fractional
rounding). The resulting file can be consumed directly by OpenROAD's
`read_saif` (and any other STA tool that takes SAIF v2 backward).

The converter is intentionally minimal — it doesn't try to model glitch
power, X-propagation, or per-cell pin activity. It's adequate for
gate-level `report_power` driven by realistic simulation stimulus.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pywellen

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)


def _iter_vars(scope, h):
    """Yield non-parameter, non-memory-element vars in this scope.

    FST exposes memory array elements as vars whose `name(h)` starts
    with `[` (the bracketed index, parent scope is the array name).
    These don't correspond to gate-level nets in the synth netlist and
    confuse the SAIF parser when nested under INSTANCE, so we skip them.
    """
    for v in scope.vars(h):
        if v.var_type() == "Parameter":
            continue
        if v.name(h).startswith("["):
            continue
        yield v


def _max_time(w: pywellen.Waveform) -> int:
    h = w.hierarchy
    mx = 0

    def walk(scope):
        nonlocal mx
        for v in _iter_vars(scope, h):
            sig = w.get_signal(v)
            for t, _ in sig.all_changes():
                if t > mx:
                    mx = t
        for s in scope.scopes(h):
            walk(s)

    for s in h.top_scopes():
        walk(s)
    return mx


def _bit_stats(changes: list, bit: int, end_t: int) -> dict:
    """Compute T0/T1/TX/TZ time-in-state + TC toggle count for a single bit.

    Values from pywellen are ints for binary or strings for 4-state x/z.
    For ints we shift; strings are scanned character-by-character. TC
    counts only 0↔1 transitions (the standard SAIF convention).
    """
    t0 = t1 = tx = tz = 0
    tc = 0
    prev_t = 0
    prev_state: str | None = None

    for t, val in changes:
        dur = t - prev_t
        if prev_state == "0":
            t0 += dur
        elif prev_state == "1":
            t1 += dur
        elif prev_state == "x":
            tx += dur
        elif prev_state == "z":
            tz += dur

        if isinstance(val, int):
            state = "1" if ((val >> bit) & 1) else "0"
        else:
            s = str(val).lower()
            idx = len(s) - 1 - bit
            ch = s[idx] if 0 <= idx < len(s) else "x"
            state = ch if ch in "01xz" else "x"

        if prev_state is not None and state != prev_state:
            if {prev_state, state} <= {"0", "1"}:
                tc += 1
        prev_state = state
        prev_t = t

    dur = end_t - prev_t
    if prev_state == "0":
        t0 += dur
    elif prev_state == "1":
        t1 += dur
    elif prev_state == "x":
        tx += dur
    elif prev_state == "z":
        tz += dur

    return {"T0": t0, "T1": t1, "TX": tx, "TZ": tz, "TC": tc}


def _emit_net(out, indent: int, name: str, stats: dict) -> None:
    pad = "  " * indent
    out.write(f"{pad}({name}\n")
    out.write(
        f"{pad}  (T0 {stats['T0']}) (T1 {stats['T1']}) "
        f"(TX {stats['TX']}) (TZ {stats['TZ']})\n"
    )
    out.write(f"{pad}  (TC {stats['TC']})\n")
    out.write(f"{pad}  (IG 0)\n")
    out.write(f"{pad})\n")


def _emit_scope(out, w: pywellen.Waveform, scope, h, indent: int, end_t: int) -> None:
    pad = "  " * indent
    out.write(f"{pad}(INSTANCE {scope.name(h)}\n")

    vars_here = list(_iter_vars(scope, h))
    if vars_here:
        out.write(f"{pad}  (NET\n")
        for v in vars_here:
            sig = w.get_signal(v)
            changes = list(sig.all_changes())
            width = v.bitwidth() or 1
            if width == 1:
                _emit_net(out, indent + 2, v.name(h), _bit_stats(changes, 0, end_t))
            else:
                for b in range(width):
                    _emit_net(
                        out,
                        indent + 2,
                        f"{v.name(h)}\\[{b}\\]",
                        _bit_stats(changes, b, end_t),
                    )
        out.write(f"{pad}  )\n")

    for s in scope.scopes(h):
        _emit_scope(out, w, s, h, indent + 1, end_t)

    out.write(f"{pad})\n")


def convert(trace_path: Path, saif_path: Path) -> None:
    """Convert FST/VCD at `trace_path` to SAIF v2.0 at `saif_path`.

    Raises FatalRtlBuddyError on input-not-found or pywellen open failure.
    """
    if not trace_path.is_file():
        log_event(
            logger,
            logging.ERROR,
            "saif.input_missing",
            path=str(trace_path),
        )
        raise FatalRtlBuddyError(f"trace file not found: {trace_path}")

    try:
        w = pywellen.Waveform(str(trace_path))
    except Exception as e:
        log_event(
            logger,
            logging.ERROR,
            "saif.open_failed",
            path=str(trace_path),
            error=str(e),
        )
        raise FatalRtlBuddyError(f"could not open {trace_path}: {e}") from e

    h = w.hierarchy
    ts = h.timescale()
    ts_value = int(ts.factor)
    ts_unit = str(ts.unit).lower()
    end_t = _max_time(w)

    saif_path.parent.mkdir(parents=True, exist_ok=True)
    with saif_path.open("w") as out:
        out.write("(SAIFILE\n")
        out.write('  (SAIFVERSION "2.0")\n')
        out.write('  (DIRECTION "backward")\n')
        out.write("  (DESIGN)\n")
        out.write('  (DATE "rtl_buddy saif")\n')
        out.write('  (VENDOR "rtl_buddy")\n')
        out.write('  (PROGRAM_NAME "rb saif")\n')
        out.write('  (VERSION "1.0")\n')
        out.write("  (DIVIDER /)\n")
        out.write(f"  (TIMESCALE {ts_value} {ts_unit})\n")
        out.write(f"  (DURATION {end_t})\n")
        for s in h.top_scopes():
            _emit_scope(out, w, s, h, 1, end_t)
        out.write(")\n")

    log_event(
        logger,
        logging.INFO,
        "saif.wrote",
        input=str(trace_path),
        output=str(saif_path),
        duration=end_t,
        timescale=f"{ts_value}{ts_unit}",
    )
