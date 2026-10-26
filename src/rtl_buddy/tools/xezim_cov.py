# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Collects, merges and reports xezim code coverage.

xezim writes one JSON document per run (``XEZIM_COV_DB``, kept per test as
``coverage.json``); :mod:`rtl_buddy.cov.xezim_raw` reads it into the records the
coverage model uses. This module turns the same records into the per-test and merged
:class:`~rtl_buddy.tools.vlog_cov.CoverageMetrics`, writes LCOV ``.info`` for line and
branch (so genhtml keeps working), and merges runs by summing counts in Python: xezim
ships no merge tool, and files from different runs share their structure.

Expression coverage does not exist in xezim and reads ``UNSP``.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import time
from collections import defaultdict

from ..cov.raw import BRANCH, COVER, LINE, TOGGLE
from ..cov.xezim_raw import load_xezim_doc, records_from_doc
from ..logging_utils import log_event
from .vlog_cov import METRIC_NAMES, CoverageMetrics, VlogCov, aggregate_cover_records

logger = logging.getLogger(__name__)

#: Simulator family this collector serves.
XEZIM_FAMILY = "xezim"


class _MergeTimeout(Exception):
    """The ``merge-timeout`` budget ran out between inputs."""


def _ratio(records):
    if not records:
        return None
    return sum(1 for r in records if r["hits"] > 0) / len(records)


def _absolutize(path, base_dir):
    if not path or os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(base_dir, path))


def _absolutize_doc(doc, base_dir):
    """Rewrite every relative source path in ``doc`` to an absolute one, in place.

    xezim reports paths as its filelist gave them, relative to its cwd (the run's
    artefact dir); two runs' relative spellings of one file differ, so a merge keys
    on absolute paths.
    """
    code = doc.get("code_coverage") or {}
    for group in ("design_units", "scopes"):
        for unit in code.get(group) or []:
            for key in ("statements", "branches"):
                for item in unit.get(key) or []:
                    item["file"] = _absolutize(item.get("file"), base_dir)
    for assertion in doc.get("assertions") or []:
        assertion["file"] = _absolutize(assertion.get("file"), base_dir)
    return doc


def _summary(covered, total):
    return {
        "covered": covered,
        "total": total,
        "percent": None if total == 0 else round(covered * 100.0 / total, 2),
    }


def _ordinal_keys(items, key_fn):
    """Yield ``(key + (ordinal,), item)``: the n-th item with the same key gets ordinal n."""
    seen: dict[tuple, int] = {}
    for item in items:
        key = key_fn(item)
        ordinal = seen.get(key, 0)
        seen[key] = ordinal + 1
        yield key + (ordinal,), item


def _merge_list(target, source, key_fn, merge_fn):
    """Merge ``source`` items into ``target`` by key, appending unseen ones."""
    index = dict(_ordinal_keys(target, key_fn))
    for key, item in _ordinal_keys(source, key_fn):
        existing = index.get(key)
        if existing is None:
            copied = copy.deepcopy(item)
            target.append(copied)
            index[key] = copied
        else:
            merge_fn(existing, item)


def _add_count(field):
    def merge(target, source):
        target[field] = int(target.get(field) or 0) + int(source.get(field) or 0)

    return merge


def _merge_assertion(target, source):
    _add_count("pass")(target, source)
    _add_count("fail")(target, source)


def _merge_branch(target, source):
    _merge_list(
        target.setdefault("arms", []),
        source.get("arms") or [],
        lambda arm: (arm.get("line"), arm.get("arm")),
        _add_count("count"),
    )


def _sum_vectors(a, b):
    width = max(len(a), len(b))
    return [
        (a[i] if i < len(a) else 0) + (b[i] if i < len(b) else 0) for i in range(width)
    ]


def _merge_toggle(target, source):
    target["rise"] = _sum_vectors(target.get("rise") or [], source.get("rise") or [])
    target["fall"] = _sum_vectors(target.get("fall") or [], source.get("fall") or [])
    target["width"] = max(int(target.get("width") or 0), int(source.get("width") or 0))


def _merge_unit(target, source):
    _merge_list(
        target.setdefault("statements", []),
        source.get("statements") or [],
        lambda s: (s.get("file"), s.get("line"), s.get("kind")),
        _add_count("count"),
    )
    _merge_list(
        target.setdefault("branches", []),
        source.get("branches") or [],
        lambda b: (b.get("file"), b.get("line"), b.get("kind")),
        _merge_branch,
    )
    _merge_list(
        target.setdefault("toggles", []),
        source.get("toggles") or [],
        lambda t: (t.get("signal"),),
        _merge_toggle,
    )
    target["instances"] = max(
        int(target.get("instances") or 0), int(source.get("instances") or 0)
    )


def _unit_summaries(unit):
    """Recompute a unit's ``statement``/``branch``/``toggle`` summaries as xezim counts them.

    Returns the ``(covered, total)`` triples so the caller can sum the document's.
    """
    statements = unit.get("statements") or []
    stmt = (sum(1 for s in statements if (s.get("count") or 0) > 0), len(statements))
    arms = [arm for b in unit.get("branches") or [] for arm in b.get("arms") or []]
    branch = (sum(1 for a in arms if (a.get("count") or 0) > 0), len(arms))
    # xezim scores toggle per direction: each bit's rise and fall are two points.
    tog_cov = tog_total = 0
    for toggle in unit.get("toggles") or []:
        width = int(toggle.get("width") or 0)
        rise = toggle.get("rise") or []
        fall = toggle.get("fall") or []
        tog_total += 2 * width
        tog_cov += sum(1 for v in rise[:width] if v > 0)
        tog_cov += sum(1 for v in fall[:width] if v > 0)
    unit["statement"] = _summary(*stmt)
    unit["branch"] = _summary(*branch)
    unit["toggle"] = _summary(tog_cov, tog_total)
    return stmt, branch, (tog_cov, tog_total)


def merge_xezim_docs(docs, *, deadline=None):
    """Sum xezim coverage documents into one; counts add, structure is the union.

    ``docs`` must already have absolute source paths. Raises :class:`_MergeTimeout`
    when ``deadline`` (a ``time.monotonic()`` value) passes between inputs.
    """
    merged: dict = {
        "sim_time": 0,
        "assertion_sites": 0,
        "assertion_pass_total": 0,
        "assertion_fail_total": 0,
        "assertions": [],
        "covergroups": [],
    }
    code = None
    for doc in docs:
        if deadline is not None and time.monotonic() > deadline:
            raise _MergeTimeout()
        merged["sim_time"] = max(merged["sim_time"], int(doc.get("sim_time") or 0))
        _merge_list(
            merged["assertions"],
            doc.get("assertions") or [],
            lambda a: (
                a.get("file"),
                a.get("line"),
                a.get("span_start"),
                a.get("kind"),
            ),
            _merge_assertion,
        )
        # Covergroups are kept as the first run reported them: rtl_buddy reads none.
        if not merged["covergroups"]:
            merged["covergroups"] = copy.deepcopy(doc.get("covergroups") or [])
        source_code = doc.get("code_coverage")
        if source_code is None:
            continue
        if code is None:
            code = {
                "kinds": list(source_code.get("kinds") or []),
                "scopes": [],
                "design_units": [],
            }
        for kind in source_code.get("kinds") or []:
            if kind not in code["kinds"]:
                code["kinds"].append(kind)
        _merge_list(
            code["design_units"],
            source_code.get("design_units") or [],
            lambda u: (u.get("design_unit"),),
            _merge_unit,
        )
        _merge_list(
            code["scopes"],
            source_code.get("scopes") or [],
            lambda u: (u.get("scope"), u.get("design_unit")),
            _merge_unit,
        )
    asserts = merged["assertions"]
    merged["assertion_sites"] = len(asserts)
    merged["assertion_pass_total"] = sum(int(a.get("pass") or 0) for a in asserts)
    merged["assertion_fail_total"] = sum(int(a.get("fail") or 0) for a in asserts)
    if code is not None:
        for scope in code["scopes"]:
            _unit_summaries(scope)
        totals = [[0, 0], [0, 0], [0, 0]]
        for unit in code["design_units"]:
            for idx, (covered, total) in enumerate(_unit_summaries(unit)):
                totals[idx][0] += covered
                totals[idx][1] += total
        code["statement"] = _summary(*totals[0])
        code["branch"] = _summary(*totals[1])
        code["toggle"] = _summary(*totals[2])
        merged["code_coverage"] = {
            "kinds": code["kinds"],
            "statement": code["statement"],
            "branch": code["branch"],
            "toggle": code["toggle"],
            "scopes": code["scopes"],
            "design_units": code["design_units"],
        }
    return merged


class XezimCov(VlogCov):
    """Collects and merges xezim coverage and exports LCOV and HTML.

    Shares :class:`VlogCov`'s LCOV, HTML and path helpers; replaces everything that
    would run ``verilator_coverage``.
    """

    def __init__(
        self,
        simulator_name=XEZIM_FAMILY,
        use_lcov=False,
        root_cfg=None,
        merge_jobs=None,
        merge_mem_bytes=None,
    ):
        super().__init__(
            simulator_name=simulator_name,
            use_lcov=use_lcov,
            root_cfg=root_cfg,
            merge_jobs=merge_jobs,
            merge_mem_bytes=merge_mem_bytes,
        )

    def is_supported(self):
        """Report whether this helper supports the selected simulator backend."""
        return self.simulator_name == XEZIM_FAMILY

    def _records(self, raw_path):
        doc = load_xezim_doc(raw_path)
        if doc is None:
            return None
        return records_from_doc(
            doc,
            base_dir=os.path.dirname(os.path.abspath(raw_path)),
            raw_path=raw_path,
        )

    @staticmethod
    def _metrics_from_records(metrics, records):
        by_metric = defaultdict(list)
        for record in records:
            by_metric[record["metric"]].append(record)
        metrics.line = _ratio(by_metric[LINE])
        metrics.branch = _ratio(by_metric[BRANCH])
        metrics.toggle = _ratio(by_metric[TOGGLE])
        # xezim has no expression coverage: None reads UNSP, never 0/0.
        metrics.expression = None
        metrics.functional = _ratio(by_metric[COVER])
        metrics.covers = aggregate_cover_records(
            [
                {
                    "name": r["name"],
                    "file": r["file"],
                    "line": r["line"],
                    "module": r["module"],
                    "hier": r["hier"],
                    "hits": r["hits"],
                }
                for r in by_metric[COVER]
            ]
        )
        return metrics

    def collect(self, raw_path, source_roots=None):
        """Collect per-test coverage metrics from a xezim coverage file."""
        if not self.is_supported():
            return None
        if raw_path is None or not os.path.exists(raw_path):
            log_event(
                logger,
                logging.WARNING,
                "coverage.raw_missing",
                path=raw_path,
                simulator=self.simulator_name,
            )
            return None
        records = self._records(raw_path)
        if records is None:
            log_event(
                logger,
                logging.WARNING,
                "coverage.raw_unreadable",
                path=raw_path,
                simulator=self.simulator_name,
            )
            return None
        return self._metrics_from_records(
            CoverageMetrics(raw_paths=[raw_path]), records
        )

    def _write_lcov(self, raw_path, lcov_path, source_roots=None):
        """Export a xezim coverage file's line and branch points to LCOV, then normalize ``SF:`` paths.

        LCOV is line-granular: a line's ``DA:`` count is the highest of its statements'.
        Each branch construct is one ``BRDA`` block on the construct's line (the arms'
        own lines can hold no statement, which genhtml rejects), numbered per file in
        source order, its arms numbered in xezim's order. A construct line with no
        statement gets a ``DA:`` carrying the arms' summed count.
        """
        doc = load_xezim_doc(raw_path)
        if doc is None:
            log_event(
                logger,
                logging.ERROR,
                "coverage.lcov_export.failed",
                simulator=self.simulator_name,
                raw_path=raw_path,
                lcov_path=lcov_path,
                returncode=None,
                stderr="unreadable xezim coverage file",
                stdout="",
            )
            return False
        _absolutize_doc(doc, os.path.dirname(os.path.abspath(raw_path)))
        lines: dict[str, dict[int, int]] = defaultdict(dict)
        # file -> {(construct line, kind, ordinal on line): [arm counts]}
        constructs: dict[str, dict[tuple, list[int]]] = defaultdict(dict)
        for unit in (doc.get("code_coverage") or {}).get("design_units") or []:
            for stmt in unit.get("statements") or []:
                file, line_no = stmt.get("file"), stmt.get("line")
                if not file or not line_no:
                    continue
                per_file = lines[file]
                per_file[line_no] = max(
                    per_file.get(line_no, 0), int(stmt.get("count") or 0)
                )
            keyed = _ordinal_keys(
                unit.get("branches") or [],
                lambda b: (b.get("file"), b.get("line"), b.get("kind")),
            )
            for (file, line_no, kind, ordinal), branch in keyed:
                if not file or not line_no:
                    continue
                counts = [int(a.get("count") or 0) for a in branch.get("arms") or []]
                key = (line_no, kind or "", ordinal)
                previous = constructs[file].get(key)
                if previous is not None and len(previous) == len(counts):
                    counts = [a + b for a, b in zip(previous, counts)]
                constructs[file][key] = counts
        for file, per_file in constructs.items():
            for (line_no, _kind, _ordinal), counts in per_file.items():
                if line_no not in lines[file]:
                    lines[file][line_no] = sum(counts)
        with open(lcov_path, "w", encoding="utf-8") as out:
            for file in sorted(set(lines) | set(constructs)):
                out.write("TN:\n")
                out.write(f"SF:{file}\n")
                per_line = lines.get(file, {})
                for line_no in sorted(per_line):
                    out.write(f"DA:{line_no},{per_line[line_no]}\n")
                out.write(f"LF:{len(per_line)}\n")
                out.write(f"LH:{sum(1 for c in per_line.values() if c > 0)}\n")
                found = hit = 0
                for block, key in enumerate(sorted(constructs.get(file, {}))):
                    for index, count in enumerate(constructs[file][key]):
                        out.write(f"BRDA:{key[0]},{block},{index},{count}\n")
                        found += 1
                        hit += 1 if count > 0 else 0
                out.write(f"BRF:{found}\n")
                out.write(f"BRH:{hit}\n")
                out.write("end_of_record\n")
        self._normalize_lcov_paths(lcov_path, source_roots=source_roots)
        log_event(
            logger,
            logging.INFO,
            "coverage.lcov_export.completed",
            simulator=self.simulator_name,
            raw_path=raw_path,
            lcov_path=lcov_path,
        )
        return True

    def _merge_raw(self, raw_paths, merged_path, *, outdir):
        """Sum ``raw_paths`` into ``merged_path``; return it, or None on failure.

        ``cfg-coverage`` ``merge-timeout`` bounds the whole merge, checked between inputs.
        """
        timeout = self._merge_timeout()
        deadline = None if timeout is None else time.monotonic() + timeout
        log_event(
            logger,
            logging.INFO,
            "coverage.merge.start",
            simulator=self.simulator_name,
            merged_path=merged_path,
            inputs=raw_paths,
            command="python count-sum of xezim coverage files",
            timeout=timeout,
            shards=1,
            jobs=1,
        )
        timed_out = False
        error = ""
        try:
            docs = []
            for path in raw_paths:
                if deadline is not None and time.monotonic() > deadline:
                    raise _MergeTimeout()
                doc = load_xezim_doc(path)
                if doc is None:
                    raise ValueError(f"unreadable xezim coverage file: {path}")
                docs.append(
                    _absolutize_doc(doc, os.path.dirname(os.path.abspath(path)))
                )
            merged = merge_xezim_docs(docs, deadline=deadline)
            with open(merged_path, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, indent=1)
                fh.write("\n")
        except _MergeTimeout:
            timed_out = True
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            error = str(exc)
        else:
            log_event(
                logger,
                logging.INFO,
                "coverage.merge.completed",
                simulator=self.simulator_name,
                merged_path=merged_path,
                inputs=raw_paths,
                shards=1,
            )
            return merged_path
        log_event(
            logger,
            logging.ERROR,
            "coverage.merge.failed",
            simulator=self.simulator_name,
            merged_path=merged_path,
            inputs=list(raw_paths),
            returncode=None,
            timeout=timeout if timed_out else None,
            stderr=error,
            stdout="",
        )
        return None

    def merge(
        self,
        raw_paths,
        outdir,
        merge_basename="coverage_merged",
        html_output=False,
        source_roots=None,
        html_outdir=None,
    ):
        """Merge xezim coverage files and return the aggregate metrics.

        Every metric comes from the merged JSON. If the merge fails, line and branch
        survive only when ``use-lcov`` or HTML output is on (from the merged per-test
        LCOV); toggle and functional are lost and listed in ``failed_metrics``.
        Expression is never measured, so it stays ``UNSP`` either way.

        Returns None only when nothing was measured and nothing failed.
        """
        if not self.is_supported():
            return None

        raw_paths = [p for p in raw_paths if p is not None and os.path.exists(p)]
        if len(raw_paths) == 0:
            return None

        merged_path = self._merge_raw(
            raw_paths, os.path.join(outdir, f"{merge_basename}.json"), outdir=outdir
        )
        metrics = CoverageMetrics(raw_paths=list(raw_paths), merged_path=merged_path)

        if self.use_lcov or html_output:
            with tempfile.TemporaryDirectory(prefix="rtl_buddy_merge_lcov_") as tmpdir:
                lcov_inputs = []
                for idx, raw_path in enumerate(raw_paths):
                    lcov_input = os.path.join(tmpdir, f"part_{idx}.info")
                    lcov_source_roots = [os.path.dirname(raw_path)]
                    if source_roots is not None:
                        lcov_source_roots.extend(source_roots)
                    if self._write_lcov(
                        raw_path, lcov_input, source_roots=lcov_source_roots
                    ):
                        lcov_inputs.append(lcov_input)
                if lcov_inputs:
                    merged_lcov_path = os.path.join(outdir, f"{merge_basename}.info")
                    self._merge_lcov_files(lcov_inputs, merged_lcov_path)
                    metrics.lcov_path = merged_lcov_path
            if metrics.lcov_path is not None and html_output:
                metrics.html_dir = self.generate_html(
                    metrics.lcov_path,
                    outdir=outdir,
                    html_dirname="coverage_merge.html",
                    html_outdir=html_outdir,
                )

        records = None if merged_path is None else self._records(merged_path)
        if records is not None:
            self._metrics_from_records(metrics, records)
        else:
            metrics.merge_failed = True
            lost = {"toggle", "functional"}
            if metrics.lcov_path is not None:
                metrics.line, metrics.branch = self._parse_lcov_summary(
                    metrics.lcov_path
                )
            else:
                lost |= {"line", "branch"}
            metrics.failed_metrics = [
                name
                for name in METRIC_NAMES
                if name in lost and getattr(metrics, name) is None
            ]

        if (
            metrics.line is None
            and metrics.branch is None
            and metrics.toggle is None
            and metrics.functional is None
            and not metrics.merge_failed
        ):
            return None
        return metrics
