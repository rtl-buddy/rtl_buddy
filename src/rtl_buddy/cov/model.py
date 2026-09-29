# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The structured coverage model: a versioned, simulator-agnostic JSON document of what a run covered.

- Each file carries its individual line, branch, toggle, expression and cover points, not only percentages. Toggle and expression detail comes from the raw database (:mod:`rtl_buddy.cov.raw`).
- Each point carries per-test hit counts (attribution) unless built with ``--coverage-model totals``; ``attribution`` records which.
- Points are keyed by project-relative source path via :mod:`rtl_buddy.cov.source_paths`.
- ``totals`` counts points with the elaborated module in their identity; ``source_totals`` drops ``module`` so a point is hit when any elaboration hit it (:func:`~rtl_buddy.cov.raw.source_point_key`). Both appear on the run, each test and each file; see ``docs/concepts/coverage.md``.

The model is written to ``cov_dir/coverage-model.json`` and referenced by ``cov_dir/manifest.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .raw import (
    BRANCH,
    COVER,
    LINE,
    METRICS,
    parse_raw_records,
    point_key,
    source_point_key,
)
from .source_paths import SourcePathResolver

#: Bumped when the document's shape changes incompatibly; adding a key is compatible.
MODEL_SCHEMA_VERSION = 1

#: ``--coverage-model`` values: the whole document, without per-point ``tests`` maps, or none (manifest only).
MODEL_MODE_FULL = "full"
MODEL_MODE_TOTALS = "totals"
MODEL_MODE_NONE = "none"
MODEL_MODES = (MODEL_MODE_FULL, MODEL_MODE_TOTALS, MODEL_MODE_NONE)

# Defined in `tools.artifact_paths` and re-exported here.
from ..tools.artifact_paths import (  # noqa: E402
    COV_MODEL_NAME as MODEL_FILENAME,
)


@dataclass(frozen=True)
class TestArtefacts:
    """One test's coverage artefacts.

    ``source_roots`` is the ``[run dir, suite root]`` hint pair for the source-path resolver. ``raw`` is preferred over ``info``.
    """

    name: str
    raw: str | None = None
    info: str | None = None
    suite: str | None = None
    source_roots: tuple[str, ...] = ()


# Stops pytest collecting this class.
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

    def source_key(self, metric: str) -> tuple:
        """This point's :func:`source_point_key` tuple."""
        return source_point_key(
            {
                "metric": metric,
                "line": self.line,
                "column": self.column,
                "name": self.name,
            }
        )

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


def _totals_from_hits(hits_by_id: dict) -> dict:
    """Totals over ``{(path, metric, key): summed hits}``: found per id, hit per id with any hits."""
    totals = _empty_totals()
    for (_path, metric, _key), hits in hits_by_id.items():
        bucket = totals[metric]
        bucket["found"] += 1
        if hits > 0:
            bucket["hit"] += 1
    for metric in METRICS:
        bucket = totals[metric]
        bucket["ratio"] = _ratio(bucket["found"], bucket["hit"])
    return totals


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
    tests,
    *,
    project_root,
    simulator: str | None = None,
    merged_info=None,
    attribution: bool = True,
) -> dict:
    """Build the coverage model from a run's per-test artefacts.

    :param tests: iterable of :class:`TestArtefacts`.
    :param project_root: source paths are reported relative to it.
    :param simulator: simulator family that produced the artefacts.
    :param merged_info: merged ``.info`` used only when no test produced any point; it has no attribution.
    :param attribution: record per-test hit counts per point. False drops only the per-point ``tests`` maps.
    """
    project_root = str(Path(project_root).resolve())
    files: dict[str, _FileEntry] = {}
    test_rows: list[dict] = []

    for artefacts in tests:
        records = _records_for(artefacts, project_root)
        if records is None:
            continue
        test_name = artefacts.name if attribution else None
        totals = _empty_totals()
        # Hits collapsed on source identity, for this test's `source_totals`.
        source_hits: dict[tuple, int] = {}
        for path, metric, record in records:
            entry = files.get(path)
            if entry is None:
                entry = files[path] = _FileEntry(path)
            entry.add(metric, record, test_name)
            bucket = totals[metric]
            bucket["found"] += 1
            if record.get("hits", 0) > 0:
                bucket["hit"] += 1
            source_id = (path, metric, source_point_key(record))
            source_hits[source_id] = source_hits.get(source_id, 0) + record.get(
                "hits", 0
            )
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
                "source_totals": _totals_from_hits(source_hits),
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
    # Summable across files: a source identity never spans two files.
    run_source_totals = _empty_totals()
    modules: dict[str, set[str]] = {}
    for path in sorted(files):
        entry = files[path]
        row = _file_row(entry)
        _sum_totals(totals, row["totals"])
        _sum_totals(run_source_totals, row["source_totals"])
        file_rows.append(row)
        for module in entry.modules:
            modules.setdefault(module, set()).add(path)

    return {
        "schema_version": MODEL_SCHEMA_VERSION,
        "generator": _generator(),
        "simulator": simulator,
        "attribution": attribution,
        "totals": totals,
        "source_totals": run_source_totals,
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
        "source_totals": _empty_totals(),
    }
    for metric in METRICS:
        ordered = sorted(
            entry.points[metric].items(), key=lambda item: _sort_key(item[0])
        )
        points = [point.as_dict(metric) for _, point in ordered]
        row[metric] = points
        row["totals"][metric] = _totals_entry(
            len(points), sum(1 for point in points if point["hits"] > 0)
        )
        # Regroup without the module, hits summed.
        collapsed: dict[tuple, int] = {}
        for _, point in ordered:
            key = point.source_key(metric)
            collapsed[key] = collapsed.get(key, 0) + point.hits
        row["source_totals"][metric] = _totals_entry(
            len(collapsed), sum(1 for hits in collapsed.values() if hits > 0)
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
    """Return ``(path, metric, record)`` rows for one test, from the raw database else the ``.info``; None if neither exists."""
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
    """Line and branch points from an LCOV ``.info`` file, which has no toggle, expression or cover detail."""
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


def source_totals(row: dict) -> dict | None:
    """A model, test row or file row's ``source_totals``, or None when the document has none.

    Consumers omit the key rather than substitute ``totals``.
    """
    totals = row.get("source_totals")
    return totals or None


def cover_points(model: dict) -> list[dict]:
    """Observed SVA cover points, with per-test hits under ``tests``, sorted by file, line, name, module."""
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
