"""Convert an FST or VCD trace to SAIF v2.0 (backward) using pywellen.

Emits per-bit T0/T1/TX/TZ/TC in the trace's own timescale. Glitch power, X-propagation and per-pin
activity are not modelled.

Each net and instance name is written as :func:`~rtl_buddy.tools.saif_names.saif_name` spells it,
so OpenSTA's `read_saif` reads it. A record whose name has no such spelling is left out, with
everything beneath it for an instance, and the count is reported: one such name would otherwise
fail the whole read.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pywellen

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .pywellen_compat import require_random_access_api
from .saif_names import saif_name

logger = logging.getLogger(__name__)


def _iter_vars(scope):
    """Yield the scope's vars, skipping parameters and memory array elements (names starting with ``[``)."""
    for v in scope.vars():
        if v.var_type == "Parameter":
            continue
        if v.name.startswith("["):
            continue
        yield v


def _max_time(w: pywellen.Waveform) -> int:
    mx = 0

    def walk(scope):
        nonlocal mx
        for v in _iter_vars(scope):
            # Index the materialised list: Signal itself rejects negative indices.
            changes = v.signal[:]
            if changes and changes[-1][0] > mx:
                mx = changes[-1][0]
        for s in scope.scopes():
            walk(s)

    for s in w.scopes():
        walk(s)
    return mx


def _bit_stats(changes: list, bit: int, end_t: int) -> dict:
    """Return T0/T1/TX/TZ time-in-state and the TC toggle count for one bit.

    TC counts only 0<->1 transitions. Values are ints (binary) or strings (4-state).
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


class _Skipped:
    """Records left out because `read_saif` cannot read their names."""

    # Original names reported in the warning; the counts cover the rest.
    EXAMPLES = 5

    def __init__(self):
        self.nets = 0
        self.instances = 0
        self.examples: list[str] = []

    def add(self, name: str, *, instance: bool, records: int = 1) -> None:
        if instance:
            self.instances += 1
        else:
            self.nets += records
        if len(self.examples) < self.EXAMPLES:
            self.examples.append(name)


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


def _emit_scope(out, scope, indent: int, end_t: int, skipped: _Skipped) -> None:
    if not scope.name.strip():
        # pywellen's unnamed scope holding a VCD array's elements, which `_iter_vars` skips.
        return
    name = saif_name(scope.name, net=False)
    if name is None:
        # The nets and scopes beneath it go too: the reader could not place them.
        skipped.add(scope.name, instance=True)
        return
    pad = "  " * indent
    out.write(f"{pad}(INSTANCE {name}\n")

    nets = []
    for v in _iter_vars(scope):
        width = v.bitwidth or 1
        if width == 1:
            written = saif_name(v.name, net=True)
            if written is None:
                skipped.add(v.name, instance=False)
                continue
            nets.append((v, [(written, 0)]))
            continue
        bits = []
        for b in range(width):
            written = saif_name(f"{v.name}[{b}]", net=True)
            if written is None:
                break
            bits.append((written, b))
        else:
            nets.append((v, bits))
            continue
        # One unwritable bit means the base name is unwritable, so every bit is.
        skipped.add(v.name, instance=False, records=width)

    if nets:
        out.write(f"{pad}  (NET\n")
        for v, bits in nets:
            changes = v.signal[:]
            for written, b in bits:
                _emit_net(out, indent + 2, written, _bit_stats(changes, b, end_t))
        out.write(f"{pad}  )\n")

    for s in scope.scopes():
        _emit_scope(out, s, indent + 1, end_t, skipped)

    out.write(f"{pad})\n")


def convert(trace_path: Path, saif_path: Path) -> None:
    """Convert the FST/VCD at `trace_path` to SAIF v2.0 at `saif_path`.

    Raises FatalRtlBuddyError if the trace is missing or unreadable, or if pywellen lacks the
    required Waveform API.
    """
    if not trace_path.is_file():
        log_event(
            logger,
            logging.ERROR,
            "saif.input_missing",
            path=str(trace_path),
        )
        raise FatalRtlBuddyError(f"trace file not found: {trace_path}")

    require_random_access_api("rb saif")

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

    try:
        ts = w.timescale
        ts_value = int(ts.factor)
        ts_unit = str(ts.unit).lower()
        end_t = _max_time(w)
    except Exception as e:
        log_event(
            logger,
            logging.ERROR,
            "saif.read_failed",
            path=str(trace_path),
            error=str(e),
        )
        raise FatalRtlBuddyError(
            f"could not read waveform from {trace_path}: {e}"
        ) from e

    saif_path.parent.mkdir(parents=True, exist_ok=True)
    skipped = _Skipped()
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
        for s in w.scopes():
            _emit_scope(out, s, 1, end_t, skipped)
        out.write(")\n")

    if skipped.nets or skipped.instances:
        log_event(
            logger,
            logging.WARNING,
            "saif.names_skipped",
            output=str(saif_path),
            nets=skipped.nets,
            instances=skipped.instances,
            examples=skipped.examples,
        )

    log_event(
        logger,
        logging.INFO,
        "saif.wrote",
        input=str(trace_path),
        output=str(saif_path),
        duration=end_t,
        timescale=f"{ts_value}{ts_unit}",
        skipped_nets=skipped.nets,
        skipped_instances=skipped.instances,
    )
