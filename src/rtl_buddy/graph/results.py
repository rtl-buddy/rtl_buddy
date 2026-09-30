# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Regression-results overlay for the design knowledge graph.

`graph.json` never holds status, seeds or artefact paths. They live in
`artefacts/graph/results-overlay.json`, keyed by the config tier's test node ids
(`test:<suite dir>#<name>`), and are joined with `load_overlay` and `overlay_for_node`.
The overlay is built from result envelopes and the artefact layout, with no clock of its
own, so a refresh with nothing re-run rewrites identical bytes. It also carries the
coverage join.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from ..config.suite import SuiteConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..runner.result_io import load_result_json
from ..tools.artifact_paths import (
    RESULT_JSON_NAME,
    run_artifact_root,
    sanitize_artifact_component,
)
from ..tools.spec_trace import _walk_yaml_files
from ..tools.wave_trace import TRACE_CANDIDATES
from .config_tier import (
    DEFAULT_FLOW,
    GRAPH_JSON_NAME,
    GRAPH_META_NAME,
    default_graph_dir,
    test_id,
)
from .coverage import COVERAGE_SOURCE_AUTO, CoverageJoin, join_coverage


logger = logging.getLogger(__name__)

# Overlay file name, written next to `graph.json`.
# Defined in `tools.artifact_paths`, the bottom of the import graph; re-exported here
# for consumers.
from ..tools.artifact_paths import (  # noqa: E402
    RESULTS_OVERLAY_NAME as RESULTS_OVERLAY_NAME,
)

# `rtl-buddy-filetype` marker, so a loader can reject the wrong file.
OVERLAY_FILETYPE = "graph_results_overlay"

# Bumped when an entry's shape changes incompatibly.
OVERLAY_SCHEMA_VERSION = 1

# Status of a test with artefacts but no result envelope; `TestResults` never produces
# it.
UNKNOWN = "UNKNOWN"

# Provenance of one entry: from a result envelope, or from artefacts only.
FROM_ENVELOPE = "result-envelope"
FROM_ARTEFACTS = "artefacts"

# Directories under `<suite>/artefacts/` that are not a test's workspace: other
# commands' per-suite roots and graph/coverage output.
_NON_TEST_DIRS = frozenset({"hier", "axi", "graph", "cov", "coverage"})

# Result-envelope file names in one run scope: `result.json` from the in-process runner,
# `dispatch/result-<tag>.json` from `rb _test-job`.
_RESULT_JSON = RESULT_JSON_NAME
_DISPATCH_DIR = "dispatch"


def _flows_of(flow: object) -> set[str]:
    """Return the `flow` attribute (a string or a list) as a set."""
    if flow is None:
        return {DEFAULT_FLOW}
    if isinstance(flow, str):
        return {flow}
    if isinstance(flow, (list, tuple)):
        return {str(f) for f in flow}
    return {DEFAULT_FLOW}


def _tool_version() -> str:
    try:
        return version("rtl-buddy")
    except PackageNotFoundError:  # pragma: no cover - only in odd installs
        return "0+unknown"


def _rel(project_root: Path, path: str | os.PathLike) -> str:
    """Return a repo-relative posix path."""
    resolved = Path(os.path.realpath(str(path)))
    root = Path(os.path.realpath(str(project_root)))
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def _iso(epoch: float) -> str:
    """Return a UTC ISO-8601 stamp for a file mtime, to the second."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def results_overlay_path(
    project_root: str | os.PathLike,
    out_dir: str | os.PathLike | None = None,
    run_tag: str | None = None,
) -> Path:
    """Return `<out dir or artefacts/graph>/results-overlay.json`.

    `run_tag` selects the overlay of one tagged run.
    """
    base = (
        Path(out_dir)
        if out_dir is not None
        else default_graph_dir(project_root, run_tag)
    )
    return base / RESULTS_OVERLAY_NAME


# ---------------------------------------------------------------------------
# Scanning one run scope
# ---------------------------------------------------------------------------


@dataclass
class _Scope:
    """One run's directory: the test root, or one `run-NNNN` under it."""

    run_id: int | None
    directory: Path
    envelope_path: Path | None = None
    envelope: dict | None = None
    error: str | None = None


def _job_tag(run_id: int | None) -> str:
    """Return the dispatch envelope tag for a run id, mirroring `_dispatch_suite_submit`.

    It names one job's envelope inside `dispatch/`, unlike `--run-tag`, which names the
    whole artefact tree.
    """
    return "single" if run_id is None else f"{run_id:04d}"


def _envelope_candidates(test_dir: Path, scope_dir: Path, run_id: int | None):
    """Return envelope paths for one run scope, most authoritative first."""
    return [
        scope_dir / _RESULT_JSON,
        test_dir / _DISPATCH_DIR / f"result-{_job_tag(run_id)}.json",
    ]


def _load_envelope(scope: _Scope, test_dir: Path) -> None:
    """Attach the newest readable envelope for `scope`, or its error.

    The in-process writer and the dispatch job can both write one; the newest mtime
    wins.
    """
    best: tuple[float, Path, dict] | None = None
    for path in _envelope_candidates(test_dir, scope.directory, scope.run_id):
        if not path.is_file():
            continue
        try:
            envelope = load_result_json(path)
        except FatalRtlBuddyError as exc:
            scope.error = str(exc)
            continue
        stamp = path.stat().st_mtime
        if best is None or stamp > best[0]:
            best = (stamp, path, envelope)
    if best is not None:
        scope.envelope_path = best[1]
        scope.envelope = best[2]
        scope.error = None


def _artefacts(project_root: Path, scope_dir: Path, test_dir: Path) -> dict:
    """Return repo-relative paths of the artefacts this run scope has.

    Only existing files are listed. Compile outputs live in the test root even for a
    `run-NNNN` scope, because one compile feeds every iteration.
    """
    found: dict[str, str] = {"dir": _rel(project_root, scope_dir)}
    for key, name in (
        ("log", "test.log"),
        ("err", "test.err"),
        ("randseed", "test.randseed"),
        ("coverage", "coverage.dat"),
    ):
        candidate = scope_dir / name
        if candidate.is_file():
            found[key] = _rel(project_root, candidate)
    # Whichever dumper ran last wins, as for `rb wave` and `rb axi-profile`.
    traces = [
        scope_dir / name for name in TRACE_CANDIDATES if (scope_dir / name).is_file()
    ]
    if traces:
        newest = max(traces, key=lambda p: p.stat().st_mtime)
        found["trace"] = _rel(project_root, newest)
    # `compile.log` (test-scoped) and `compile.retry.log` (run-scoped) stay separate:
    # they fail for different reasons, and only the run whose retry failed wrote one.
    compile_log = test_dir / "compile.log"
    if compile_log.is_file():
        found["compile_log"] = _rel(project_root, compile_log)
    retry_log = scope_dir / "compile.retry.log"
    if retry_log.is_file():
        found["compile_retry_log"] = _rel(project_root, retry_log)
    return found


def _read_randseed(scope_dir: Path) -> int | str | None:
    """Return the first line of `test.randseed`, the seed the sim ran with.

    It lets an agent replay a failure with `rb randtest -r N`.
    """
    path = scope_dir / "test.randseed"
    try:
        with open(path, "r") as handle:
            line = handle.readline().strip()
    except OSError:
        return None
    if not line:
        return None
    try:
        return int(line)
    except ValueError:
        return line


def _scope_timestamp(scope: _Scope, artefacts: dict, project_root: Path) -> str | None:
    """Return when this run happened, taken from files, never the clock.

    The envelope's mtime is used; without an envelope the newest listed artefact is the
    proxy and the entry says so via `source`.
    """
    if scope.envelope_path is not None:
        return _iso(scope.envelope_path.stat().st_mtime)
    stamps = []
    for key, rel in artefacts.items():
        if key == "dir":
            continue
        candidate = project_root / rel
        try:
            stamps.append(candidate.stat().st_mtime)
        except OSError:  # pragma: no cover - raced deletion
            continue
    return _iso(max(stamps)) if stamps else None


def _scope_entry(project_root: Path, scope: _Scope, test_dir: Path) -> dict:
    """Return one run's overlay record."""
    artefacts = _artefacts(project_root, scope.directory, test_dir)
    entry: dict = {
        "run_id": scope.run_id,
        "status": UNKNOWN,
        "source": FROM_ARTEFACTS,
    }
    if scope.envelope is not None:
        results = scope.envelope["result"].results
        entry["status"] = results.get("result") or UNKNOWN
        entry["desc"] = results.get("desc")
        entry["run_token"] = scope.envelope.get("run_token")
        entry["rtl_buddy_version"] = scope.envelope.get("rtl_buddy_version")
        entry["source"] = FROM_ENVELOPE
        entry["result_json"] = _rel(project_root, scope.envelope_path)
        if scope.envelope.get("run_id") is not None:
            entry["run_id"] = scope.envelope["run_id"]
        compile_record = results.get("compile")
        if isinstance(compile_record, dict):
            # Only the three promised fields, in fixed order, and only when the envelope
            # carries them, so a refresh with nothing re-run stays byte-identical.
            block = {
                "duration_sec": compile_record.get("duration_sec"),
                "builder": compile_record.get("builder"),
                "reused": compile_record.get("reused"),
            }
            # An all-null block (left by a config whose `prepare()` failed in the build
            # job) is dropped; the entry-level None filter does not reach nested values.
            if any(v is not None for v in block.values()):
                entry["compile"] = block
    seed = _read_randseed(scope.directory)
    if seed is not None:
        entry["randseed"] = seed
    stamp = _scope_timestamp(scope, artefacts, project_root)
    if stamp is not None:
        entry["timestamp"] = stamp
    entry["artefacts"] = artefacts
    return {k: v for k, v in entry.items() if v is not None}


def _scopes(test_dir: Path) -> list[_Scope]:
    """Return the run scopes under one test artefact dir, base run first.

    `run-NNNN` is the `randtest` layout; a plain `rb test` writes into the test root.
    """
    scopes = [_Scope(run_id=None, directory=test_dir)]
    for child in sorted(test_dir.iterdir()):
        if not child.is_dir() or not child.name.startswith("run-"):
            continue
        try:
            run_id = int(child.name[len("run-") :])
        except ValueError:
            continue
        scopes.append(_Scope(run_id=run_id, directory=child))
    return scopes


def _is_test_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    name = path.name
    if name.startswith(".") or name.startswith("obj_dir"):
        return False
    return name not in _NON_TEST_DIRS


# ---------------------------------------------------------------------------
# Scanning a project
# ---------------------------------------------------------------------------


@dataclass
class ResultsOverlay:
    """Result of one overlay refresh.

    Attributes:
      overlay: The `results-overlay.json` payload.
      entries: The `tests` block, keyed by test node id.
      problems: Envelopes that could not be read.
      unmatched: Overlay ids with no node in the graph (only when a graph was supplied).
      missing: Test nodes in that graph with no result.
      path: Where the overlay was written, once it has been.
      coverage: The coverage join; None means coverage was not asked for.
    """

    overlay: dict
    entries: dict = dc_field(default_factory=dict)
    problems: list[dict] = dc_field(default_factory=list)
    unmatched: list[str] = dc_field(default_factory=list)
    missing: list[str] = dc_field(default_factory=list)
    path: Path | None = None
    coverage: CoverageJoin | None = None

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries.values():
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        return dict(sorted(counts.items()))

    def with_results(self) -> int:
        return sum(1 for e in self.entries.values() if e.get("source") == FROM_ENVELOPE)

    def coverage_summary(self) -> dict | None:
        """Return the coverage block's summary, or None."""
        block = (self.coverage.block if self.coverage else None) or {}
        return block.get("summary")


def _declared_test_names(tests_yaml: str) -> dict[str, str]:
    """Return `sanitized artefact dir name -> declared test name` for one suite.

    Read through `SuiteConfig`, as `rb test` does. Needed only when an envelope is
    missing, since an envelope carries the real (possibly sweep-expanded) name.
    """
    try:
        suite = SuiteConfig(tests_yaml)
    except FatalRtlBuddyError:
        return {}
    names: dict[str, str] = {}
    for test in suite.get_tests():
        names.setdefault(sanitize_artifact_component(test.get_name()), test.get_name())
    return names


def _test_name_for(test_dir: Path, scopes: list[_Scope], declared: dict[str, str]):
    """Return the real test name behind a sanitized artefact directory.

    The envelope is authoritative. Otherwise the declared-name map is used, and finally
    the directory name.
    """
    for scope in scopes:
        if scope.envelope and scope.envelope.get("test"):
            return scope.envelope["test"]
    return declared.get(test_dir.name, test_dir.name)


def collect_results(
    project_root: str | os.PathLike,
    *,
    verif_dir: str | os.PathLike | None = None,
    graph: dict | None = None,
    coverage: bool | str = True,
    cov_dir: str | os.PathLike | None = None,
    cov_manifest: str | os.PathLike | None = None,
    run_tag: str | None = None,
) -> ResultsOverlay:
    """Scan every suite's artefacts and build the results overlay.

    Never raises for a broken envelope; it lands in `problems`.

    Args:
      project_root: Directory holding `root_config.yaml`; ids and paths are relative to
        it.
      verif_dir: Tree searched for `tests.yaml`. Defaults to `<project_root>/verif`, as
        the config tier does.
      graph: A loaded `graph.json`. When given, entries are cross-checked (`in_graph`) and
        declared tests with no result are listed in `missing`.
      coverage: `True` or `"auto"` reads the newest `cov_dir/manifest.json` and falls back
        to per-test raw databases; `"model"` reads the manifest only; any other string is a
        merged LCOV `.info` path; `False` skips the join. A tree with no coverage artefacts
        leaves the `coverage` block absent.
      cov_dir, cov_manifest: Read coverage from here instead. Naming either makes a read
        failure a reported problem.
      run_tag: Scan one `--run-tag` tree (`<suite>/artefacts/.runs/<tag>/`) instead of the
        flat one.

    Returns:
      The payload plus the bookkeeping the CLI reports.
    """
    root = Path(os.path.realpath(str(project_root)))
    search_verif = Path(verif_dir) if verif_dir is not None else root / "verif"

    entries: dict[str, dict] = {}
    problems: list[dict] = []

    for tests_yaml in _walk_yaml_files(str(search_verif), "tests.yaml"):
        suite_dir = Path(tests_yaml).parent
        suite_rel = _rel(root, suite_dir)
        artefact_root = run_artifact_root(suite_dir, run_tag)
        if not artefact_root.is_dir():
            continue
        declared = _declared_test_names(tests_yaml)

        for test_dir in sorted(artefact_root.iterdir()):
            if not _is_test_dir(test_dir):
                continue
            scopes = _scopes(test_dir)
            for scope in scopes:
                _load_envelope(scope, test_dir)
                if scope.error:
                    problems.append(
                        {
                            "suite": suite_rel,
                            "dir": _rel(root, scope.directory),
                            "error": scope.error,
                        }
                    )
            name = _test_name_for(test_dir, scopes, declared)
            entry = _test_entry(root, suite_rel, name, test_dir, scopes)
            if entry is None:
                continue
            entries[entry["id"]] = entry

    unmatched: list[str] = []
    missing: list[str] = []
    if graph is not None:
        # Simulation tests only: synthesis, formal, CDC and FPGA runs leave no
        # `result.json`, so counting them as `missing` would fail `--strict`. A node
        # with no `flow` counts as simulation.
        graph_tests = {
            node["id"]
            for node in graph.get("nodes") or []
            if node.get("type") == "test"
            and _flows_of(node.get("flow")) <= {DEFAULT_FLOW}
        }
        for node_id, entry in entries.items():
            entry["in_graph"] = node_id in graph_tests
            if not entry["in_graph"]:
                unmatched.append(node_id)
        missing = sorted(graph_tests - set(entries))

    ordered = {k: entries[k] for k in sorted(entries)}
    join = None
    if coverage:
        source = COVERAGE_SOURCE_AUTO if coverage is True else str(coverage)
        join = join_coverage(
            root,
            entries=ordered,
            graph=graph,
            cov_dir=cov_dir,
            manifest=cov_manifest,
            source=source,
        )
        problems.extend(join.problems)
        # Beside `artefacts.coverage`, which is only a path to the raw database.
        for node_id, scalars in join.per_test.items():
            entry = ordered.get(node_id)
            if entry is not None:
                entry["coverage"] = scalars

    overlay = {
        "rtl-buddy-filetype": OVERLAY_FILETYPE,
        "schema_version": OVERLAY_SCHEMA_VERSION,
        "generated_by": {
            "tool": "rtl_buddy",
            "version": _tool_version(),
            "command": "graph results",
        },
        "keyed_by": "test node id",
        # Placed before the long per-test block so the verdict reads first; filled in
        # below, and the key position is fixed here.
        "summary": {},
        "tests": ordered,
    }
    if join is not None and join.block is not None:
        overlay["coverage"] = join.block
    result = ResultsOverlay(
        overlay=overlay,
        entries=overlay["tests"],
        problems=problems,
        unmatched=sorted(unmatched),
        missing=missing,
        coverage=join,
    )
    overlay["summary"] = {
        "tests": len(result.entries),
        "with_results": result.with_results(),
        "statuses": result.status_counts(),
        "unmatched": result.unmatched,
        "missing": result.missing,
        "problems": problems,
    }
    if result.coverage_summary() is not None:
        overlay["summary"]["coverage"] = result.coverage_summary()
    log_event(
        logger,
        logging.DEBUG,
        "graph_results.collected",
        tests=len(result.entries),
        with_results=result.with_results(),
        problems=len(problems),
    )
    return result


def _test_entry(
    project_root: Path,
    suite_rel: str,
    test_name: str,
    test_dir: Path,
    scopes: list[_Scope],
) -> dict | None:
    """Fold one test's run scopes into a single overlay entry, or None if this is not a
    test directory.

    The top level is the last run (newest timestamp, then highest `run_id`); every
    iteration stays under `runs`. A scope with no envelope and no recognized artefact
    contributes nothing, which keeps other commands' per-suite workspaces out;
    `_is_test_dir` names the known ones.
    """
    records = []
    for scope in scopes:
        record = _scope_entry(project_root, scope, test_dir)
        if record["source"] != FROM_ENVELOPE and len(record["artefacts"]) <= 1:
            continue
        records.append(record)
    if not records:
        return None

    def _key(record: dict) -> tuple[str, int]:
        return (record.get("timestamp") or "", record.get("run_id") or 0)

    last = max(records, key=_key)
    entry = {
        "id": test_id(suite_rel, test_name),
        "suite": suite_rel,
        "test": test_name,
        **{k: v for k, v in last.items() if k != "run_id"},
        "run_id": last.get("run_id"),
    }
    if len(records) > 1 or last.get("run_id") is not None:
        entry["runs"] = sorted(records, key=lambda r: r.get("run_id") or 0)
    return entry


# -----------------------------------------------------------------------
# Writing and loading: the join hooks used by `rb graph query`
# -----------------------------------------------------------------------


def write_overlay(overlay: dict, path: str | os.PathLike) -> Path:
    """Write the overlay atomically, creating parent directories.

    Output is stable and sorted.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(overlay, ensure_ascii=True, indent=2) + "\n")
    os.replace(tmp, target)
    return target


def load_overlay(path: str | os.PathLike) -> dict | None:
    """Load an overlay, or None when there is none.

    Accepts the file, the directory holding it, or a project root. Never raises; an
    absent, unreadable or foreign file means no results are known.
    """
    candidate = Path(path)
    if candidate.is_dir():
        direct = candidate / RESULTS_OVERLAY_NAME
        candidate = direct if direct.is_file() else results_overlay_path(candidate)
    try:
        payload = json.loads(candidate.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("rtl-buddy-filetype") != OVERLAY_FILETYPE
        or payload.get("schema_version") != OVERLAY_SCHEMA_VERSION
    ):
        log_event(
            logger,
            logging.WARNING,
            "graph_results.overlay_rejected",
            path=str(candidate),
            filetype=payload.get("rtl-buddy-filetype")
            if isinstance(payload, dict)
            else None,
            schema_version=payload.get("schema_version")
            if isinstance(payload, dict)
            else None,
        )
        return None
    return payload


def overlay_for_node(overlay: dict | None, node_id: str) -> dict | None:
    """Return the overlay entry for one node id, or None.

    Non-test nodes have no entry.
    """
    if not overlay:
        return None
    return (overlay.get("tests") or {}).get(node_id)


def annotate_graph(graph: dict, overlay: dict | None) -> int:
    """Attach overlay entries to a graph's nodes in memory; return the count.

    The caller's dict is mutated, never the file. Do not write the result back to
    `graph.json`.
    """
    if not overlay:
        return 0
    annotated = 0
    for node in graph.get("nodes") or []:
        entry = overlay_for_node(overlay, node.get("id"))
        if entry is None:
            continue
        node["results"] = entry
        annotated += 1
    return annotated


def graph_linkage(graph_dir: str | os.PathLike) -> dict:
    """Return which graph this overlay was refreshed against.

    Carries the `graph-meta.json` fingerprint so a stale overlay can be told from a
    fresh one.
    """
    directory = Path(graph_dir)
    graph_file = directory / GRAPH_JSON_NAME
    linkage: dict = {"path": GRAPH_JSON_NAME, "present": graph_file.is_file()}
    try:
        meta = json.loads((directory / GRAPH_META_NAME).read_text())
    except (OSError, json.JSONDecodeError):
        return linkage
    if isinstance(meta, dict) and meta.get("fingerprint"):
        linkage["fingerprint"] = meta["fingerprint"]
    return linkage


def refresh_results_overlay(
    project_root: str | os.PathLike,
    *,
    verif_dir: str | os.PathLike | None = None,
    out_dir: str | os.PathLike | None = None,
    graph_path: str | os.PathLike | None = None,
    coverage: bool | str = True,
    cov_dir: str | os.PathLike | None = None,
    cov_manifest: str | os.PathLike | None = None,
    run_tag: str | None = None,
) -> ResultsOverlay:
    """Collect results and write `results-overlay.json`.

    The call behind `rb graph results`. `graph.json` is read and never written.
    `run_tag` points the refresh at one run's tree and writes the overlay into that
    run's graph directory; `graph.json` is read from the untagged `artefacts/graph/`
    unless `--graph` names another.
    """
    root = Path(os.path.realpath(str(project_root)))
    out = Path(out_dir) if out_dir is not None else default_graph_dir(root, run_tag)
    graph_file = (
        Path(graph_path)
        if graph_path is not None
        else default_graph_dir(root) / GRAPH_JSON_NAME
    )
    graph = None
    try:
        loaded = json.loads(graph_file.read_text())
        if isinstance(loaded, dict):
            graph = loaded
    except (OSError, json.JSONDecodeError):
        graph = None

    result = collect_results(
        root,
        verif_dir=verif_dir,
        graph=graph,
        coverage=coverage,
        cov_dir=cov_dir,
        cov_manifest=cov_manifest,
        run_tag=run_tag,
    )
    linkage = {**graph_linkage(graph_file.parent), "path": _rel(root, graph_file)}
    # Rebuilt so the linkage sits with the header keys and the two long blocks
    # (coverage, tests) come last.
    collected = result.overlay
    header = {
        k: v for k, v in collected.items() if k not in ("summary", "tests", "coverage")
    }
    result.overlay = {
        **header,
        "graph": linkage,
        "summary": collected["summary"],
        **({"coverage": collected["coverage"]} if "coverage" in collected else {}),
        "tests": collected["tests"],
    }
    target = write_overlay(result.overlay, out / RESULTS_OVERLAY_NAME)
    result.path = target
    log_event(
        logger,
        logging.INFO,
        "graph_results.written",
        overlay=str(target),
        tests=len(result.entries),
        with_results=result.with_results(),
    )
    return result
