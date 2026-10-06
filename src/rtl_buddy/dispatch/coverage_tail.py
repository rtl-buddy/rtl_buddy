# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The coverage tail as one dispatched job: its input spec and result envelope.

Under a scheduler-backed backend the head writes the collected per-test coverage and the
:meth:`~rtl_buddy.tools.coverage.CoverageReporter.build_metadata` arguments to a spec
file, ``rb _cov-job`` runs the merge, model build, LCOV exports and manifest on a
compute node, and the head reads back each call's display lines and machine payload.

The tail reads only each test's name and ``coverage`` dict, so the spec carries those
and nothing else. Post-processing mutates the per-test ``coverage`` dicts (LCOV paths,
HTML directories); the job returns them so the head can update its results.
"""

from __future__ import annotations

import json
import os
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

from ..errors import FatalRtlBuddyError
from ..tools.artifact_paths import (
    COV_MANIFEST_NAME,
    COV_MODEL_NAME,
    atomic_tmp_name,
)

COV_TAIL_SPEC_FILETYPE = "coverage_tail_spec"
COV_TAIL_RESULT_FILETYPE = "coverage_tail_result"
COV_TAIL_SCHEMA_VERSION = 1

#: ``build_metadata`` keyword arguments the spec carries verbatim; all JSON-safe.
_PLAIN_KWARGS = (
    "outdir",
    "suite_name",
    "coverage_merge",
    "coverage_merge_raw",
    "coverage_html",
    "coverage_coverview",
    "coverage_per_test",
    "coverage_merge_info_process",
    "source_roots",
    "dir_summary_paths",
    "source_summary",
    "command",
    "model_mode",
)


#: ``build_metadata`` flags that each ask for heavy work (a merge, LCOV/HTML/Coverview
#: exports, or a source-point summary built from the model).
_HEAVY_FLAGS = (
    "coverage_merge",
    "coverage_merge_raw",
    "coverage_merge_info_process",
    "coverage_html",
    "coverage_coverview",
    "dir_summary_paths",
    "source_summary",
)


def is_manifest_only(call) -> bool:
    """Whether a tail call asks only for the manifest: no merge, no export, no model file.

    Such a call (``--coverage-model none`` and no merge or export flag) is cheap enough
    to run on the submit host, so the head keeps it in-process instead of submitting a job.
    """
    if call.get("model_mode") != "none":
        return False
    return not any(call.get(flag) for flag in _HEAVY_FLAGS)


def clear_previous_outputs(calls) -> list[str]:
    """Remove the previous run's ``cov_dir/manifest.json`` and ``coverage-model.json`` for every call.

    Called before the tail job is submitted, so a tail that leaves no answer leaves no
    manifest either: discovery (newest ``cov_dir/manifest.json``) must not present the
    previous run's verdict as this run's. Returns the removed paths.
    """
    removed = []
    seen = set()
    for call in calls:
        cov_dir = os.path.join(str(call["outdir"]), "cov_dir")
        for name in (COV_MANIFEST_NAME, COV_MODEL_NAME):
            path = os.path.join(cov_dir, name)
            if path in seen:
                continue
            seen.add(path)
            try:
                os.remove(path)
            except FileNotFoundError:
                continue
            removed.append(path)
    return removed


def _write_json(path, document) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(atomic_tmp_name(path.name))
    # `default=str`: a path a tool left in a coverage dict travels as its text.
    tmp.write_text(
        json.dumps(document, ensure_ascii=True, indent=2, default=str) + "\n"
    )
    os.replace(tmp, path)
    return path


def _read_json(path, filetype):
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as e:
        raise FatalRtlBuddyError(f"coverage tail: {path} is missing") from e
    except (OSError, json.JSONDecodeError) as e:
        raise FatalRtlBuddyError(f"coverage tail: cannot read {path}: {e}") from e
    if not isinstance(raw, dict) or raw.get("rtl-buddy-filetype") != filetype:
        raise FatalRtlBuddyError(f"coverage tail: {path} is not a {filetype} file")
    if raw.get("schema_version") != COV_TAIL_SCHEMA_VERSION:
        raise FatalRtlBuddyError(
            f"coverage tail: unsupported schema_version "
            f"{raw.get('schema_version')!r} in {path} "
            f"(expected {COV_TAIL_SCHEMA_VERSION})"
        )
    return raw


class TailTests:
    """The head's per-test results, indexed once so every call and suite shares them.

    The same result object can appear in a call's ``suite_results`` and in a suite of
    its ``reg_results``; the spec lists it once, so an update made by one call is seen
    by the next, as in-process.
    """

    def __init__(self):
        self._index: dict[int, int] = {}
        self.results: list = []
        self.rows: list[dict] = []

    def index_of(self, suite_result) -> int:
        res = suite_result["results"]
        key = id(res)
        if key not in self._index:
            self._index[key] = len(self.rows)
            self.results.append(res)
            self.rows.append(
                {
                    "test_name": suite_result["test_name"],
                    "coverage": res.results.get("coverage"),
                }
            )
        return self._index[key]


def write_tail_spec(path, calls, *, run_token) -> tuple[Path, list]:
    """Write the spec for ``calls``.

    Each call is a dict of ``build_metadata`` keyword arguments; ``suite_results`` and
    ``reg_results`` hold the head's live result objects.
    Returns ``(path, results)``: ``results[i]`` is the object the job's ``tests[i]``
    coverage update belongs to.
    """
    tests = TailTests()
    spec_calls = []
    for call in calls:
        entry = {name: call.get(name) for name in _PLAIN_KWARGS if name in call}
        entry["tests"] = [tests.index_of(row) for row in call["suite_results"]]
        reg_results = call.get("reg_results")
        if reg_results is not None:
            entry["reg_results"] = [
                {
                    "test_suite": suite["test_suite"],
                    "test_suite_path": suite.get("test_suite_path"),
                    "tests": [tests.index_of(row) for row in suite["results"]],
                }
                for suite in reg_results
            ]
        spec_calls.append(entry)
    document = {
        "rtl-buddy-filetype": COV_TAIL_SPEC_FILETYPE,
        "schema_version": COV_TAIL_SCHEMA_VERSION,
        "rtl_buddy_version": version("rtl-buddy"),
        "run_token": run_token,
        "tests": tests.rows,
        "calls": spec_calls,
    }
    return _write_json(path, document), tests.results


def load_tail_spec(path) -> tuple[str | None, list[dict], list[dict]]:
    """Read a spec back as ``(run_token, calls, tests)``.

    Each call is a ``build_metadata`` keyword dict with ``suite_results`` and
    ``reg_results`` rebuilt as the shapes the tail reads: ``{"test_name", "results"}``
    rows whose ``results.results`` holds the test's ``coverage`` dict. ``tests`` lists
    each test's ``coverage`` dict, which the calls mutate in place.
    """
    raw = _read_json(path, COV_TAIL_SPEC_FILETYPE)
    rows = []
    coverages = []
    for test in raw.get("tests") or []:
        coverage = test.get("coverage")
        results = {} if coverage is None else {"coverage": coverage}
        coverages.append(coverage)
        rows.append(
            {
                "test_name": test.get("test_name"),
                "results": SimpleNamespace(results=results),
            }
        )
    calls = []
    for entry in raw.get("calls") or []:
        call = {name: entry[name] for name in _PLAIN_KWARGS if name in entry}
        call["suite_results"] = [rows[i] for i in entry.get("tests") or []]
        if "reg_results" in entry:
            call["reg_results"] = [
                {
                    "test_suite": suite.get("test_suite"),
                    "test_suite_path": suite.get("test_suite_path"),
                    "results": [rows[i] for i in suite.get("tests") or []],
                }
                for suite in entry["reg_results"]
            ]
        calls.append(call)
    return raw.get("run_token"), calls, coverages


def write_tail_result(path, *, run_token, outcomes, tests) -> Path:
    """Write the job's answer: one ``{metadata, coverage}`` per call, and every test's ``coverage`` dict after the calls ran."""
    return _write_json(
        path,
        {
            "rtl-buddy-filetype": COV_TAIL_RESULT_FILETYPE,
            "schema_version": COV_TAIL_SCHEMA_VERSION,
            "rtl_buddy_version": version("rtl-buddy"),
            "run_token": run_token,
            "calls": [
                {"metadata": list(metadata), "coverage": coverage}
                for metadata, coverage in outcomes
            ],
            "tests": list(tests),
        },
    )


def load_tail_result(path, *, expected_run_token, expected_calls, expected_tests):
    """Read the job's answer as ``(outcomes, tests)``.

    Raises :class:`FatalRtlBuddyError` for a missing, malformed or stale file (another
    run's token) or one that does not answer every call and test.
    """
    raw = _read_json(path, COV_TAIL_RESULT_FILETYPE)
    if raw.get("run_token") != expected_run_token:
        raise FatalRtlBuddyError(
            f"coverage tail: {path} is from a different run (run_token "
            f"{raw.get('run_token')!r} != {expected_run_token!r})"
        )
    calls = raw.get("calls")
    tests = raw.get("tests")
    if (
        not isinstance(calls, list)
        or len(calls) != expected_calls
        or not isinstance(tests, list)
        or len(tests) != expected_tests
    ):
        raise FatalRtlBuddyError(
            f"coverage tail: {path} does not answer the {expected_calls} call(s) "
            f"and {expected_tests} test(s) it was given"
        )
    outcomes = []
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("coverage"), dict):
            raise FatalRtlBuddyError(f"coverage tail: {path} has a malformed call")
        outcomes.append((list(call.get("metadata") or []), call["coverage"]))
    return outcomes, tests
