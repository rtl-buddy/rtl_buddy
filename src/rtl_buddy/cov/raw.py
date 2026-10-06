# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
r"""Reader for Verilator's raw coverage database.

``coverage.dat`` is a text file: a one-line header followed by one
record per counter::

    C '\x01f\x02tb_top.sv\x01l\x0214\x01n\x0217\x01t\x02user\x01page\x02v_user/tb_top\x01o\x02APB_IF_WRITE\x01h\x02tb_top.APB_IF_WRITE' 3

Keys are ``\x01<key>\x02<value>`` pairs: ``f`` file, ``l`` line, ``n``
column, ``t`` type, ``page`` ``v_<type>/<module>``, ``o`` the coverage
comment (an SVA label for a user point, the signal for a toggle point,
the keyword for a branch point), ``h`` the hierarchy path. Unknown keys
are ignored.

Only the raw database keeps toggle, expression and user detail; ``verilator_coverage --write-info`` folds them into anonymous ``DA:`` records.

Verilator writes one record per source point per containing module, not per instance: instances arrive merged, counts summed and the differing hierarchy component replaced by ``*``. The same source line in two modules stays two records, so ``module`` is part of a point's identity.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Canonical metric names; Verilator's spellings (``user``, ``expr``) map onto these.
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
    """Extract the module from a ``page`` key written ``v_<type>/<module>``; other values pass through."""
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

    Returns one dict per record, ``{metric, type, name, file, line, column, module, hier, hits}``, or None when the file cannot be read. `metrics` restricts the result to those canonical names.

    A record ends at the last ``' `` before the count, so a quote inside a comment does not truncate it.
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
    """Per-elaboration identity of a point within one file: ``(line, column, name, module)``.

    The same for every metric, so a file or run counts each record of the merged database once, as a test does. Two line points on one source line (an ``if`` and its ``else``, or one line elaborated in two modules) are two points. See :func:`source_point_key`.
    """
    return (
        record["line"],
        record["column"],
        record["name"],
        record["module"],
    )


def source_point_key(record: dict) -> tuple:
    """Source identity of a point: :func:`point_key` without ``module``.

    Elaborations of one source point collapse into one point with summed hits, so it is covered when any elaboration hit it. ``column`` and ``name`` stay in the key, or one line's toggle bits, or its ``if`` and ``else`` blocks, would merge.
    """
    return (
        record["line"],
        record["column"],
        record["name"],
    )
