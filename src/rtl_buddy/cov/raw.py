# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
r"""Reader for Verilator's raw coverage database (#399).

``coverage.dat`` is a text file: a one-line header followed by one
record per counter::

    C '\x01f\x02tb_top.sv\x01l\x0214\x01n\x0217\x01t\x02user\x01page\x02v_user/tb_top\x01o\x02APB_IF_WRITE\x01h\x02tb_top.APB_IF_WRITE' 3

Keys are ``\x01<key>\x02<value>`` pairs: ``f`` file, ``l`` line, ``n``
column, ``t`` type, ``page`` ``v_<type>/<module>``, ``o`` the coverage
comment (an SVA label for a user point, the signal for a toggle point,
the keyword for a branch point), ``h`` the hierarchy path. Unknown keys
are ignored.

The raw database is the **only** place the detail survives.
``verilator_coverage --write-info`` folds toggle, expression and user
points into anonymous ``DA:`` records, erasing the signal names, the
expression terms and the SVA labels alike — which is why per-signal
toggle detail could not previously be reported even though it was
generated on every run.

Verilator writes one record per source point per containing *module*,
not per instance: a point instantiated many times arrives already
merged, with counts summed and the differing hierarchy component
replaced by ``*``. It keeps the same source line apart when it is
compiled into more than one module (an ``include``d cover property,
say), which is why ``module`` is part of every point's identity here.
Verified against Verilator 5.049 output.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Canonical metric names. Verilator's own spellings are mapped onto
#: these so a consumer never has to know that user coverage is called
#: ``user`` in the database and ``functional`` in the summary, or that
#: expression coverage is written ``expr`` by some versions.
LINE = "line"
BRANCH = "branch"
TOGGLE = "toggle"
EXPRESSION = "expression"
COVER = "cover"

METRICS = (LINE, BRANCH, TOGGLE, EXPRESSION, COVER)

_TYPE_ALIASES = {
    "line": LINE,
    "branch": BRANCH,
    "toggle": TOGGLE,
    "expr": EXPRESSION,
    "expression": EXPRESSION,
    "user": COVER,
}

_PAGE_PREFIX_RE = re.compile(r"^v_[A-Za-z0-9_]+/")


def canonical_metric(record_type: str | None) -> str | None:
    """Map a raw ``t=`` value onto a canonical metric name, or None."""
    if not record_type:
        return None
    return _TYPE_ALIASES.get(record_type)


def module_from_page(page: str | None) -> str | None:
    """Extract the containing module from a record's ``page`` key.

    Pages are written ``v_<type>/<module>``; anything else is passed
    through as-is rather than guessed at.
    """
    if not page:
        return None
    return _PAGE_PREFIX_RE.sub("", page, count=1) or None


def parse_record_keys(key_blob: bytes) -> dict:
    r"""Split a record's comment key into its ``\x01<key>\x02<value>`` pairs."""
    keys = {}
    for chunk in key_blob.split(b"\x01"):
        if not chunk:
            continue
        key, sep, value = chunk.partition(b"\x02")
        if not sep:
            continue
        keys[key.decode("utf-8", errors="replace")] = value.decode(
            "utf-8", errors="replace"
        )
    return keys


def parse_raw_records(raw_path, *, metrics=None) -> list[dict] | None:
    """Parse every counter record out of a raw coverage database.

    Returns one dict per record —
    ``{metric, type, name, file, line, column, module, hier, hits}`` —
    or None when the file cannot be read. ``metrics`` restricts the
    result to the given canonical metric names.

    Records are split on line boundaries and on the *last* ``' `` before
    the count, so a comment containing a quote (a labelled expression
    term, for instance) does not truncate the record.
    """
    try:
        raw_bytes = Path(raw_path).read_bytes()
    except OSError:
        return None

    wanted = None if metrics is None else set(metrics)
    records = []
    for raw_line in raw_bytes.split(b"\n"):
        if not raw_line.startswith(b"C '"):
            continue
        split_at = raw_line.rfind(b"' ")
        if split_at < 0:
            continue
        try:
            hits = int(raw_line[split_at + 2 :].strip())
        except ValueError:
            continue
        keys = parse_record_keys(raw_line[3:split_at])
        metric = canonical_metric(keys.get("t"))
        if metric is None or (wanted is not None and metric not in wanted):
            continue
        records.append(_record(keys, metric, hits))
    return records


def _record(keys: dict, metric: str, hits: int) -> dict:
    hier = keys.get("h") or None
    name = keys.get("o") or None
    if name is None and hier is not None:
        # Verilator appends the label as the last hierarchy segment.
        name = hier.rsplit(".", 1)[-1]
    return {
        "metric": metric,
        "type": keys.get("t"),
        "name": name,
        "file": keys.get("f") or None,
        "line": _int_or_none(keys.get("l")),
        "column": _int_or_none(keys.get("n")),
        "module": module_from_page(keys.get("page")),
        "hier": hier,
        "hits": hits,
    }


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def point_key(record: dict) -> tuple:
    """Identity of a coverage point within one file.

    Line coverage is keyed on the line alone — a source line is hit or
    it is not. Everything else keys on ``(line, column, name, module)``:
    several toggle points share a line (one per bit), several branch
    arms share a line, and one cover property compiled into two modules
    is two points, not one.
    """
    if record["metric"] == LINE:
        return (record["line"],)
    return (
        record["line"],
        record["column"],
        record["name"],
        record["module"],
    )
