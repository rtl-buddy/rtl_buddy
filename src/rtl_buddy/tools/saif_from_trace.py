"""Convert an FST or VCD trace to SAIF v2.0 (backward) using pywellen.

Emits per-bit T0/T1/TX/TZ/TC in the trace's own timescale. Glitch power, X-propagation and per-pin
activity are not modelled.

The trace is read in one pass: `Waveform.stream_changes` delivers every change of every signal in
time order, and a per-bit accumulator (:class:`_Activity`) keeps only the running counts. Reading
each variable with its own ``var.signal[:]`` costs one pass over the file per signal, which on a
gate-level FST with hundreds of thousands of variables takes about a day (rtl-buddy/rtl_buddy#269).
The trace's end time is the last change the same pass sees.

Memory: the converter keeps one accumulator per signal, whatever the trace length. pywellen still
loads the trace's time table when it opens the file (``stream_only`` does not avoid it), so the
process grows with the number of time steps: about 110 MB above baseline for a 5 M-step FST, against about 900 MB for the per-variable read.

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


def _all_vars(w: pywellen.Waveform) -> list:
    """Every var `_iter_vars` yields, in every scope: the signals whose changes set the end time."""
    found: list = []

    def walk(scope):
        found.extend(_iter_vars(scope))
        for s in scope.scopes():
            walk(s)

    for s in w.scopes():
        walk(s)
    return found


def _signal_key(signal_id) -> str:
    """A value key for a pywellen ``SignalId``.

    ``SignalId`` compares and hashes by object identity, and each access returns a new object, so
    a var's ``signal_ref`` and the id ``stream_changes`` passes never match as objects. Their
    ``str`` (``SignalId(<n>)``) does.
    """
    return str(signal_id)


#: Time-in-state slot of each bit state in :class:`_Activity`'s counters.
_SLOT = {"0": 0, "1": 1, "x": 2, "z": 3}


def _bit_state(val, bit: int) -> str:
    """Return bit ``bit`` of a change value as ``0``, ``1``, ``x`` or ``z``.

    An int is a binary value; a string is 4-state, MSB first, and a bit it does not cover or a
    character other than 0/1/x/z reads as ``x``.
    """
    if isinstance(val, int):
        return "1" if ((val >> bit) & 1) else "0"
    s = str(val).lower()
    idx = len(s) - 1 - bit
    ch = s[idx] if 0 <= idx < len(s) else "x"
    return ch if ch in "01xz" else "x"


def _bit_states(val, width: int) -> list[str]:
    """Every bit of a change value, LSB first, each as :func:`_bit_state` reads it."""
    if isinstance(val, int):
        return ["1" if (val >> bit) & 1 else "0" for bit in range(width)]
    text = str(val).lower()
    n = len(text)
    return [
        (text[n - 1 - bit] if text[n - 1 - bit] in "01xz" else "x") if bit < n else "x"
        for bit in range(width)
    ]


#: `_Activity1.value` before the first change; equal to no change value.
_NO_VALUE = object()


class _Activity:
    """Running T0/T1/TX/TZ/TC of each bit of one signal, fed one change at a time.

    The same totals as a pass over the signal's whole change list, without holding the list: time
    before the first change is in no state, only a 0<->1 transition is a toggle, and
    :meth:`stats` closes each bit's last state at the trace's end. A bit's time is added when its
    state changes, not at every change of the signal, so a change of a wide bus costs the bits
    that moved; with two binary values those are found by XOR. :class:`_Activity1` is the one-bit
    case, which a gate-level trace is almost entirely made of.

    A change whose value equals the previous one is dropped, as ``var.signal[:]`` drops it: a VCD
    ``$dumpall`` or ``$dumpvars`` snapshot repeats every value, and counting it would move the
    trace's end time. An event's repeats are its triggers and are kept, as ``signal[:]`` keeps
    them. :meth:`change` returns whether the change counted.
    """

    __slots__ = ("width", "event", "value", "since", "states", "time", "toggles")

    def __init__(self, width: int, event: bool = False):
        self.width = width
        self.event = event
        # The last change's value; None before the first change.
        self.value = None
        # Per bit: when it entered its current state, the state, and its time in [0, 1, x, z].
        self.since = [0] * width
        self.states: list[str] = ["x"] * width
        self.time = [[0, 0, 0, 0] for _ in range(width)]
        self.toggles = [0] * width

    def change(self, t: int, val) -> bool:
        prev_val = self.value
        if prev_val is not None and val == prev_val and not self.event:
            return False
        self.value = val
        states = self.states
        if prev_val is None:
            self.states = _bit_states(val, self.width)
            self.since = [t] * self.width
            return True
        since = self.since
        time = self.time
        if val.__class__ is int and prev_val.__class__ is int:
            moved = (prev_val ^ val) & ((1 << self.width) - 1)
            while moved:
                low = moved & -moved
                moved ^= low
                bit = low.bit_length() - 1
                old = states[bit]
                time[bit][_SLOT[old]] += t - since[bit]
                since[bit] = t
                states[bit] = "0" if old == "1" else "1"
                self.toggles[bit] += 1
            return True
        for bit, state in enumerate(_bit_states(val, self.width)):
            old = states[bit]
            if state != old:
                time[bit][_SLOT[old]] += t - since[bit]
                since[bit] = t
                states[bit] = state
                if old in "01" and state in "01":
                    self.toggles[bit] += 1
        return True

    def stats(self, bit: int, end_t: int) -> dict:
        if self.value is None:
            return _close(self.time[bit], None, self.toggles[bit], 0)
        return _close(
            self.time[bit], self.states[bit], self.toggles[bit], end_t - self.since[bit]
        )


class _Activity1:
    """:class:`_Activity` for a one-bit signal, with the per-change work inlined."""

    __slots__ = ("width", "event", "value", "prev_t", "state", "time", "toggles")

    def __init__(self, event: bool = False):
        self.width = 1
        self.event = event
        # The last counted value; a sentinel before the first change.
        self.value = _NO_VALUE
        self.prev_t = 0
        self.state: str | None = None
        self.time = [0, 0, 0, 0]
        self.toggles = 0

    def change(self, t: int, val) -> bool:
        if val == self.value and not self.event:
            return False
        self.value = val
        if val.__class__ is int:
            state = "1" if val & 1 else "0"
        else:
            state = _bit_state(val, 0)
        prev = self.state
        if prev is not None:
            self.time[_SLOT[prev]] += t - self.prev_t
            if state != prev and prev in "01" and state in "01":
                self.toggles += 1
        self.state = state
        self.prev_t = t
        return True

    def stats(self, bit: int, end_t: int) -> dict:
        return _close(self.time, self.state, self.toggles, end_t - self.prev_t)


def _close(time: list, state: str | None, toggles: int, tail: int) -> dict:
    """One bit's SAIF record: ``time`` in [0, 1, x, z] plus ``tail`` in its last ``state``."""
    t0, t1, tx, tz = time
    if state == "0":
        t0 += tail
    elif state == "1":
        t1 += tail
    elif state == "x":
        tx += tail
    elif state == "z":
        tz += tail
    return {"T0": t0, "T1": t1, "TX": tx, "TZ": tz, "TC": toggles}


def _new_activity(width: int, event: bool = False):
    return _Activity1(event) if width == 1 else _Activity(width, event)


def _read_activity(w: pywellen.Waveform, variables: list) -> tuple[dict, int]:
    """Stream every change of ``variables`` once; return ``({signal key: accumulator}, end time)``.

    The end time is the last counted change of any of them (repeats of an unchanged value are
    not counted, see :class:`_Activity`), the trace's `DURATION`. Vars that alias one
    signal share its accumulator. ``stream_changes`` consumes the Waveform's change data, so this
    is its one read; the hierarchy stays usable afterwards.
    """
    activity: dict = {}
    for v in variables:
        key = _signal_key(v.signal_ref)
        width = v.bitwidth or 1
        held = activity.get(key)
        if held is None or held.width < width:
            activity[key] = _new_activity(width, event=v.var_type == "Event")
    # Bound methods, looked up once: the callback runs once per change in the trace.
    handlers = {key: acc.change for key, acc in activity.items()}
    get = handlers.get
    end_t = 0

    def on_change(t, signal_id, val):
        nonlocal end_t
        handler = get(str(signal_id))
        # Only a change that counted moves the end, as only those are in `signal[:]`.
        if handler is not None and handler(t, val) and t > end_t:
            end_t = t

    if variables:
        w.stream_changes(on_change, variables)
    return activity, end_t


def _bit_stats(changes: list, bit: int, end_t: int) -> dict:
    """Return T0/T1/TX/TZ time-in-state and the TC toggle count for one bit of a change list.

    TC counts only 0<->1 transitions. Values are ints (binary) or strings (4-state).
    """
    acc = _new_activity(1) if bit == 0 else _Activity(bit + 1)
    for t, val in changes:
        acc.change(t, val)
    return acc.stats(bit, end_t)


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


def _emit_scope(
    out, scope, indent: int, end_t: int, skipped: _Skipped, activity: dict
) -> None:
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
            acc = activity[_signal_key(v.signal_ref)]
            for written, b in bits:
                _emit_net(out, indent + 2, written, acc.stats(b, end_t))
        out.write(f"{pad}  )\n")

    for s in scope.scopes():
        _emit_scope(out, s, indent + 1, end_t, skipped, activity)

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

    require_random_access_api("rb saif", streaming=True)

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
        activity, end_t = _read_activity(w, _all_vars(w))
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
            _emit_scope(out, s, 1, end_t, skipped, activity)
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
