"""The latch borrow report of `rb pnr`'s final reports: `latch_borrow.rpt` and the `latch_borrow_margin_ps` result field.

OpenSTA gives a latch data pin whose data arrives while the latch is open a setup slack of exactly 0 and hands the borrowed time on to the paths that start at the latch, so the design's worst setup slack reads 0 whatever margin the latch has left. The margin appears only in `report_checks`, under "Time Borrowing Information": `max time borrow` (`user max time borrow` with `set_max_time_borrow`) and `actual time borrow`. OpenSTA has no property for either, and `report_checks -format json` leaves the block out.

The final reports write each latch data pin's setup check to `latch_borrow.rpt`, per corner (`report_tcl`), and rb reads the figures from it (`parse`). An endpoint's margin is its slack plus the time it may still borrow, `slack + max - actual`: for a borrowing latch, whose slack is 0, that is `max - actual`; for one whose data arrives before it opens, its slack plus the whole window; for one whose data arrives after it closes, its negative slack.
"""

import re
from dataclasses import dataclass
from importlib.resources import files

#: Printed by the final reports once per corner reported: the corner, then the latch data pins.
TAG = "RB-LATCH-BORROW:"
#: Starts each corner's part of the report file, followed by the corner.
HEADER = "Latch D endpoints at corner"
#: The report file, next to `timing.rpt`.
REPORT = "latch_borrow.rpt"
#: The corner a single-corner platform's report names; rb reads it as the platform's corner.
SINGLE_CORNER = "-"

_TEMPLATE_PACKAGE = "rtl_buddy.pnr"
_TCL_FILE = "latch_borrow.tcl"

_TAG_RE = re.compile(rf"^{re.escape(TAG)} (\S+) (\d+)\s*$", re.M)
_HEADER_RE = re.compile(rf"^{re.escape(HEADER)} (\S+)\s*$", re.M)
_UNAVAILABLE_RE = re.compile(
    r"^rb: latch borrow report (?:at \S+ )?unavailable: .*$", re.M
)
_NUMBER = r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
_STARTPOINT_RE = re.compile(r"^Startpoint: ", re.M)
_ENDPOINT_RE = re.compile(r"^Endpoint: (\S+)", re.M)
# The data path's last pin, on the line before `data arrival time`: `delay time edge pin (cell)`.
_END_PIN_RE = re.compile(
    rf"^\s*{_NUMBER}\s+{_NUMBER}\s+[\^v]\s+(\S+)\s+\([^)]*\)\s*\n\s*{_NUMBER}\s+data arrival time",
    re.M,
)
_SLACK_RE = re.compile(rf"^\s*{_NUMBER}\s+slack \((?:MET|VIOLATED)\)", re.M)
_MAX_BORROW_RE = re.compile(rf"^(?:user )?max time borrow\s+{_NUMBER}\s*$", re.M)
_ACTUAL_BORROW_RE = re.compile(rf"^actual time borrow\s+{_NUMBER}\s*$", re.M)


@dataclass(frozen=True)
class LatchEndpoint:
    """One latch data pin's setup check, in the timer's unit."""

    pin: str
    slack: float
    max_borrow: float
    actual_borrow: float

    @property
    def margin(self) -> float:
        """How much later the data could arrive before the check fails: `slack + max - actual`."""
        return self.slack + self.max_borrow - self.actual_borrow


#: The `report_checks` option that names a corner: `-scene` on OpenSTA 3, `-corner` before it. Only a multi-corner step defines it.
_CORNER_ARGS_PROC = """\
proc rb::latch_borrow::corner_args {name} {
    if {[info commands ::sta::find_scene] ne ""} {
        return [list -scene $name]
    }
    return [list -corner $name]
}
"""


def report_tcl(corners: list[str], modes: bool = False) -> str:
    """Return the final reports' latch borrow step: each latch data pin's setup checks, per corner, into `REPORT`, with a `TAG` line per corner.

    `corners` are a multi-corner platform's corners, each reported in its own scene; a single-corner platform's one corner is reported in the default one as `SINGLE_CORNER`, since its name need not be a Tcl word. With `modes`, each corner's scene times its own SDC mode, named after the corner, which the step makes current while it reports that corner, and the mode current before is restored after. The step carries its own leading newline and no trailing one, as `corner_reports` does.
    """
    step = files(_TEMPLATE_PACKAGE).joinpath(_TCL_FILE).read_text()
    leave = ' ""'
    if corners:
        step += _CORNER_ARGS_PROC
        corner_list = " ".join(
            f"[list {c} [rb::latch_borrow::corner_args {c}]"
            + (f" {{set_mode {c}}}]" if modes else "]")
            for c in corners
        )
        if modes:
            step += "set rb_latch_borrow_mode [sta::cmd_mode_name]\n"
            leave = " {set_mode $::rb_latch_borrow_mode}"
    else:
        corner_list = f"[list {SINGLE_CORNER} {{}}]"
    return (
        '\nputs ">>> Latch borrow"\n'
        + step
        + f'rb::latch_borrow::report {TAG} "{HEADER}" '
        f"$OUT_DIR/{REPORT} [list {corner_list}]{leave}"
    )


def reported_corners(log_text: str) -> dict[str, int]:
    """The corners the step reported in `log_text`, the final reports' part of `pnr.log`, each with its latch data pin count; empty for a design without latches."""
    return {corner: int(pins) for corner, pins in _TAG_RE.findall(log_text)}


def unavailable(log_text: str) -> list[str]:
    """The lines where the step said a report failed."""
    return _UNAVAILABLE_RE.findall(log_text)


def parse_checks(text: str) -> list[LatchEndpoint]:
    """The latch setup checks in `text`, `report_checks -format full` output, in order. A path without a "Time Borrowing Information" block is left out."""
    starts = [m.start() for m in _STARTPOINT_RE.finditer(text)]
    checks = []
    for begin, end in zip(starts, [*starts[1:], len(text)]):
        block = text[begin:end]
        slack = _SLACK_RE.search(block)
        max_borrow = _MAX_BORROW_RE.search(block)
        actual = _ACTUAL_BORROW_RE.search(block)
        if not (slack and max_borrow and actual):
            continue
        pin = _END_PIN_RE.search(block)
        if pin:
            name = pin.group(3)
        else:
            endpoint = _ENDPOINT_RE.search(block)
            name = endpoint.group(1) if endpoint else "-"
        checks.append(
            LatchEndpoint(
                pin=name,
                slack=float(slack.group(1)),
                max_borrow=float(max_borrow.group(1)),
                actual_borrow=float(actual.group(1)),
            )
        )
    return checks


def parse(text: str) -> dict[str, list[LatchEndpoint]]:
    """Each corner's latch setup checks in `text`, the report file, in the file's order."""
    heads = list(_HEADER_RE.finditer(text))
    out: dict[str, list[LatchEndpoint]] = {}
    for head, nxt in zip(heads, [*heads[1:], None]):
        body = text[head.end() : nxt.start() if nxt else len(text)]
        out.setdefault(head.group(1), []).extend(parse_checks(body))
    return out


def summarize(checks: list[LatchEndpoint], ps_per_unit: float) -> dict:
    """One corner's summary: `margin_ps` and `endpoint`, the smallest margin over the checks and its pin (None without checks), `endpoints`, the pins checked, and `borrowing`, the pins with a check that borrows."""
    worst = min(checks, key=lambda c: c.margin, default=None)
    return {
        "margin_ps": worst.margin * ps_per_unit if worst else None,
        "endpoint": worst.pin if worst else None,
        "endpoints": len({c.pin for c in checks}),
        "borrowing": len({c.pin for c in checks if c.actual_borrow > 0}),
    }
