# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The structured coverage model (#399).

One versioned JSON document describing what a run covered, built from
artefacts already on disk. Its shape is **simulator-agnostic** — a file
holds points, a point has hits and an attribution — even though the
Verilator raw database plus its LCOV export is the only producer today.

Three properties are the point of the exercise:

**Per point, not per percentage.** A file carries its individual line,
branch, toggle, expression and cover points, so "which lines are cold"
is a read rather than a re-run. Toggle and expression detail exists only
in the raw database (:mod:`rtl_buddy.cov.raw`); the LCOV export folds
both into anonymous records.

**Attribution is unconditional.** Every point carries the per-test hit
counts behind it — the ``.desc`` data Coverview gets, except it is built
whenever per-test artefacts exist rather than only when packaging an
archive. That is what answers "which test covered this line", and its
inverse, "what would I lose by dropping this test".

**Paths are project-relative.** Points are keyed by repo-relative source
path via the one resolver in :mod:`rtl_buddy.cov.source_paths`, so a
model stays meaningful when the run directory is gone.

The model is written to ``cov_dir/coverage-model.json`` and pointed at
by ``cov_dir/manifest.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .raw import BRANCH, COVER, LINE, METRICS, parse_raw_records, point_key
from .source_paths import SourcePathResolver

#: Bumped when the document's shape changes incompatibly.
MODEL_SCHEMA_VERSION = 1

#: Filename inside ``cov_dir``.
# Defined in `tools.artifact_paths` — the bottom of the import graph, and
# where the artefact-clearing helpers protect it from a co-named run's
# suffix clear (#469). Re-exported here, where consumers already look.
from ..tools.artifact_paths import (  # noqa: E402
    COV_MODEL_NAME as MODEL_FILENAME,
)


@dataclass(frozen=True)
class TestArtefacts:
    """One test's coverage artefacts, as the manifest records them.

    ``source_roots`` is the ``[run dir, suite root]`` hint pair the
    source-path resolver takes; ``raw`` is preferred over ``info``
    because it carries the per-signal detail the LCOV export drops.
    """

    name: str
    raw: str | None = None
    info: str | None = None
    suite: str | None = None
    source_roots: tuple[str, ...] = ()


# Named for the RTL test it describes, not for pytest.
TestArtefacts.__test__ = False


@dataclass
class _Point:
    line: int | None
    column: int | None
    name: str | None
    module: str | None
    hits: int = 0
    tests: dict = field(default_factory=dict)

    def add(self, test: str | None, hits: int) -> None:
        self.hits += hits
        if test is not None:
            self.tests[test] = self.tests.get(test, 0) + hits

    def as_dict(self, metric: str) -> dict:
        point = {"line": self.line, "hits": self.hits}
        if metric != LINE:
            point["column"] = self.column
            point["name"] = self.name
            point["module"] = self.module
        if self.tests:
            point["tests"] = dict(sorted(self.tests.items()))
        return point


class _FileEntry:
    def __init__(self, path: str):
        self.path = path
        self.modules: set[str] = set()
        self.points: dict[str, dict[tuple, _Point]] = {m: {} for m in METRICS}

    def add(self, metric: str, record: dict, test: str | None) -> None:
        key = point_key(record)
        bucket = self.points[metric]
        point = bucket.get(key)
        if point is None:
            point = _Point(
                line=record.get("line"),
                column=record.get("column"),
                name=record.get("name"),
                module=record.get("module"),
            )
            bucket[key] = point
        point.add(test, record.get("hits", 0))
        if record.get("module"):
            self.modules.add(record["module"])


def _ratio(found: int, hit: int):
    return None if found == 0 else hit / found


def _totals_entry(found: int, hit: int) -> dict:
    return {"found": found, "hit": hit, "ratio": _ratio(found, hit)}


def _empty_totals() -> dict:
    return {metric: _totals_entry(0, 0) for metric in METRICS}


def _sum_totals(target: dict, source: dict) -> None:
    for metric in METRICS:
        entry = source[metric]
        merged = target[metric]
        merged["found"] += entry["found"]
        merged["hit"] += entry["hit"]
        merged["ratio"] = _ratio(merged["found"], merged["hit"])


def _generator() -> str:
    try:
        return f"rtl-buddy {version('rtl-buddy')}"
    except PackageNotFoundError:  # pragma: no cover - source checkout only
        return "rtl-buddy"


def build_model(
    tests, *, project_root, simulator: str | None = None, merged_info=None
) -> dict:
    """Build the coverage model from a run's per-test artefacts.

    :param tests: iterable of :class:`TestArtefacts`.
    :param project_root: project root; every source path is reported
        relative to it.
    :param simulator: simulator family that produced the artefacts.
    :param merged_info: optional merged ``.info`` used only when no test
        produced any point at all, so a merge-only tree still yields a
        model (with no attribution — a merged file has no test column).
    """
    project_root = str(Path(project_root).resolve())
    files: dict[str, _FileEntry] = {}
    test_rows: list[dict] = []

    for artefacts in tests:
        records = _records_for(artefacts, project_root)
        if records is None:
            continue
        totals = _empty_totals()
        for path, metric, record in records:
            entry = files.get(path)
            if entry is None:
                entry = files[path] = _FileEntry(path)
            entry.add(metric, record, artefacts.name)
            bucket = totals[metric]
            bucket["found"] += 1
            if record.get("hits", 0) > 0:
                bucket["hit"] += 1
        for metric in METRICS:
            bucket = totals[metric]
            bucket["ratio"] = _ratio(bucket["found"], bucket["hit"])
        test_rows.append(
            {
                "name": artefacts.name,
                "suite": artefacts.suite,
                "raw": _relative(artefacts.raw, project_root),
                "info": _relative(artefacts.info, project_root),
                "totals": totals,
            }
        )

    if not files and merged_info is not None:
        for path, metric, record in _info_records(
            merged_info, project_root, source_roots=()
        ):
            entry = files.get(path)
            if entry is None:
                entry = files[path] = _FileEntry(path)
            entry.add(metric, record, None)

    file_rows = []
    totals = _empty_totals()
    modules: dict[str, set[str]] = {}
    for path in sorted(files):
        entry = files[path]
        row = _file_row(entry)
        _sum_totals(totals, row["totals"])
        file_rows.append(row)
        for module in entry.modules:
            modules.setdefault(module, set()).add(path)

    return {
        "schema_version": MODEL_SCHEMA_VERSION,
        "generator": _generator(),
        "simulator": simulator,
        "totals": totals,
        "counts": {
            "files": len(file_rows),
            "tests": len(test_rows),
            "modules": len(modules),
        },
        "modules": {name: sorted(paths) for name, paths in sorted(modules.items())},
        "tests": sorted(test_rows, key=lambda row: row["name"]),
        "files": file_rows,
    }


def _file_row(entry: _FileEntry) -> dict:
    row = {
        "path": entry.path,
        "modules": sorted(entry.modules),
        "totals": _empty_totals(),
    }
    for metric in METRICS:
        points = [
            point.as_dict(metric)
            for _, point in sorted(
                entry.points[metric].items(), key=lambda item: _sort_key(item[0])
            )
        ]
        row[metric] = points
        row["totals"][metric] = _totals_entry(
            len(points), sum(1 for point in points if point["hits"] > 0)
        )
    return row


def _sort_key(key: tuple) -> tuple:
    return tuple((value is None, value if value is not None else "") for value in key)


def _relative(path, project_root: str) -> str | None:
    if path is None:
        return None
    try:
        return Path(path).resolve().relative_to(project_root).as_posix()
    except ValueError:
        return str(path)


def _records_for(artefacts: TestArtefacts, project_root: str):
    """Yield ``(path, metric, record)`` for one test, raw database first."""
    if artefacts.raw is not None and os.path.exists(artefacts.raw):
        rows = _raw_records(artefacts.raw, project_root, artefacts.source_roots)
        if rows:
            return rows
    if artefacts.info is not None and os.path.exists(artefacts.info):
        return _info_records(artefacts.info, project_root, artefacts.source_roots)
    return None


def _resolver(project_root: str, base_dir, source_roots) -> SourcePathResolver:
    return SourcePathResolver(
        project_root, base_dir=base_dir, source_roots=source_roots
    )


def _raw_records(raw_path: str, project_root: str, source_roots):
    records = parse_raw_records(raw_path)
    if not records:
        return []
    resolver = _resolver(project_root, os.path.dirname(raw_path), source_roots)
    resolved: dict[str, str] = {}
    rows = []
    for record in records:
        source = record.get("file")
        if source is None:
            continue
        path = resolved.get(source)
        if path is None:
            path = resolved[source] = (
                resolver.resolve(source).project_relative or source
            )
        rows.append((path, record["metric"], record))
    return rows


def _info_records(info_path: str, project_root: str, source_roots):
    """Line and branch points from an LCOV ``.info`` file.

    The fallback for a test whose raw database is gone: an ``.info``
    carries no toggle, expression or cover detail and no names.
    """
    resolver = _resolver(project_root, os.path.dirname(info_path), source_roots)
    rows = []
    current = None
    with open(info_path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if line.startswith("SF:"):
                current = resolver.resolve(line[3:].strip())
                current = current.project_relative or line[3:].strip()
            elif current is None:
                continue
            elif line.startswith("DA:"):
                payload = line[3:].split(",")
                if len(payload) < 2:
                    continue
                try:
                    line_no, hits = int(payload[0]), int(payload[1])
                except ValueError:
                    continue
                rows.append(
                    (
                        current,
                        LINE,
                        {
                            "line": line_no,
                            "column": None,
                            "name": None,
                            "module": None,
                            "hits": hits,
                            "metric": LINE,
                        },
                    )
                )
            elif line.startswith("BRDA:"):
                payload = line[5:].split(",")
                if len(payload) < 4:
                    continue
                try:
                    line_no = int(payload[0])
                except ValueError:
                    continue
                taken = payload[3]
                hits = 0 if taken in ("-", "") else int(taken)
                rows.append(
                    (
                        current,
                        BRANCH,
                        {
                            "line": line_no,
                            "column": None,
                            "name": f"{payload[1]}/{payload[2]}",
                            "module": None,
                            "hits": hits,
                            "metric": BRANCH,
                        },
                    )
                )
            elif line == "end_of_record":
                current = None
    return rows


def write_model(model: dict, cov_dir) -> str:
    """Write the model into ``cov_dir`` and return its path."""
    cov_dir = Path(cov_dir)
    cov_dir.mkdir(parents=True, exist_ok=True)
    path = cov_dir / MODEL_FILENAME
    with path.open("w", encoding="utf-8") as fh:
        json.dump(model, fh, indent=2, sort_keys=False)
        fh.write("\n")
    return str(path)


def load_model(path) -> dict:
    """Read a model document back."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def cover_points(model: dict) -> list[dict]:
    """Observed SVA cover points, attribution included.

    :func:`cover_records` is this list with the per-test column dropped.
    The graph's declared-vs-observed join (#402) needs that column —
    "which spec item is exercised" is only half the answer without "by
    which test" — so the walk lives here once rather than being repeated
    against the model's file rows by every consumer that wants it.
    """
    records = []
    for file_row in model.get("files", []):
        for point in file_row.get(COVER, []):
            record = {
                "name": point.get("name"),
                "file": file_row["path"],
                "line": point.get("line"),
                "module": point.get("module"),
                "hits": point.get("hits", 0),
            }
            tests = point.get("tests")
            if tests:
                record["tests"] = dict(sorted(tests.items()))
            records.append(record)
    return sorted(
        records,
        key=lambda r: (
            r["file"] or "",
            r["line"] if r["line"] is not None else -1,
            r["name"] or "",
            r["module"] or "",
        ),
    )


def cover_records(model: dict) -> list[dict]:
    """Observed SVA cover points, in the run-level payload's shape."""
    return [
        {key: value for key, value in record.items() if key != "tests"}
        for record in cover_points(model)
    ]
