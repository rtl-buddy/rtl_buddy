# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Regression-results overlay for the design knowledge graph (#379).

``graph.json`` answers *what exists and what covers what*. It must never
answer *what passed last night* — status, seeds and artefact paths churn
on every run, and baking them in would invalidate the build fingerprint
and re-merge every tier for a result nobody asked the graph about.

So the volatile half lives in a second file,
``artefacts/graph/results-overlay.json``, keyed by the very same test
node ids the config tier emits (``test:<suite dir>#<name>``). Refreshing
it never touches ``graph.json``; a consumer joins the two on the id (see
:func:`load_overlay` and :func:`overlay_for_node`, the hooks ``rb graph
query`` uses in #380). This is the same split the rtl-buddy-view
overlays (clock/reset/axi-perf/wave) already use: base artefact plus
refreshable side-car.

Everything here is read back off the disk the runner already writes:

* the per-run **result envelope** (:mod:`rtl_buddy.runner.result_io`) —
  status, ``run_token``, ``run_id``, and the ``rtl_buddy`` version that
  produced it. Its file mtime is the entry's timestamp: the overlay never
  stamps a wall clock of its own, so re-running ``rb graph results``
  without re-running a test rewrites the same bytes.
* the documented **artefact layout** (``docs/development/guidelines.md``)
  — ``<suite>/artefacts/<test>/`` for a single run, plus
  ``run-NNNN/`` per iteration of a ``randtest``, holding ``test.log``,
  ``test.err``, ``test.randseed``, ``coverage.dat`` and the trace.

A test directory with no envelope still gets an entry: its artefact
paths are real and useful, its status is ``UNKNOWN``. That is the
honest answer for a tree written by an rtl_buddy older than the envelope,
or a run that died before POST.

Since #402 the overlay also carries the run's **coverage** join —
per-test scalars beside the ``artefacts.coverage`` path, per-module
ratios keyed to design node ids, and a declared-vs-observed verdict per
``covitem:`` node. Those numbers are read out of the coverage model
(#399) that the run already wrote — or, since #390, synthesized from
the per-test raw databases this scan itself found, or joined by file
from a merged LCOV ``.info`` named on the command line; nothing here
ever invokes ``verilator_coverage``, which is what keeps a refresh with
nothing re-run byte-identical. See :mod:`rtl_buddy.graph.coverage`.
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
from ..tools.artifact_paths import sanitize_artifact_component
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

#: Overlay file name, written next to ``graph.json``.
RESULTS_OVERLAY_NAME = "results-overlay.json"

#: ``rtl-buddy-filetype`` marker, so a loader can reject the wrong file.
OVERLAY_FILETYPE = "graph_results_overlay"

#: Bumped whenever an entry's shape changes incompatibly.
OVERLAY_SCHEMA_VERSION = 1

#: Status recorded for a test whose artefacts exist but whose result
#: envelope does not. Never a value :class:`TestResults` itself produces.
UNKNOWN = "UNKNOWN"

#: Envelope-backed vs artefact-only provenance for one entry.
FROM_ENVELOPE = "result-envelope"
FROM_ARTEFACTS = "artefacts"

#: Directories under ``<suite>/artefacts/`` that are not a test's workspace.
#: ``hier``/``axi`` are other commands' per-suite roots (guidelines →
#: Command Roots); the dot-directories are dispatch and shared-build state.
_NON_TEST_DIRS = frozenset({"hier", "axi", "graph", "cov", "coverage"})

#: Result-envelope file names inside one run scope. ``result.json`` is the
#: in-process runner's; ``dispatch/result-<tag>.json`` is what ``rb
#: _test-job`` writes for the head to collect (#351).
_RESULT_JSON = "result.json"
_DISPATCH_DIR = "dispatch"


def _flows_of(flow: object) -> set[str]:
    """The ``flow`` attribute as a set — it is a string, or a list."""
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
    """Repo-relative, posix-separated path — the form ids and paths use."""
    resolved = Path(os.path.realpath(str(path)))
    root = Path(os.path.realpath(str(project_root)))
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def _iso(epoch: float) -> str:
    """UTC ISO-8601 stamp for a file mtime, second resolution."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def results_overlay_path(
    project_root: str | os.PathLike, out_dir: str | os.PathLike | None = None
) -> Path:
    """``<out dir or artefacts/graph>/results-overlay.json``."""
    base = Path(out_dir) if out_dir is not None else default_graph_dir(project_root)
    return base / RESULTS_OVERLAY_NAME


# ---------------------------------------------------------------------------
# Scanning one run scope
# ---------------------------------------------------------------------------


@dataclass
class _Scope:
    """One run's directory: the test root, or one ``run-NNNN`` under it."""

    run_id: int | None
    directory: Path
    envelope_path: Path | None = None
    envelope: dict | None = None
    error: str | None = None


def _run_tag(run_id: int | None) -> str:
    """Dispatch envelope tag for a run id — mirrors ``_dispatch_suite_submit``."""
    return "single" if run_id is None else f"{run_id:04d}"


def _envelope_candidates(test_dir: Path, scope_dir: Path, run_id: int | None):
    """Envelope paths for one run scope, most authoritative first."""
    return [
        scope_dir / _RESULT_JSON,
        test_dir / _DISPATCH_DIR / f"result-{_run_tag(run_id)}.json",
    ]


def _load_envelope(scope: _Scope, test_dir: Path) -> None:
    """Attach the newest readable envelope for ``scope`` (or its error).

    Both producers can be present for one run — the in-process writer
    always runs, and under ``--dispatch`` the job additionally writes the
    envelope the head collects. Newest mtime wins so a replay through
    either path is what the overlay reports.
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
    """Repo-relative paths of the artefacts this run scope actually has.

    Only existing files are listed: an absent key is the answer "this run
    produced none", which is more useful than a path that 404s. Compile
    outputs live in the test root even for a ``run-NNNN`` scope, because
    one compile feeds every iteration.
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
    # Whichever dumper ran last wins, the same rule `rb wave` and `rb
    # axi-profile` resolve a trace by.
    traces = [
        scope_dir / name for name in TRACE_CANDIDATES if (scope_dir / name).is_file()
    ]
    if traces:
        newest = max(traces, key=lambda p: p.stat().st_mtime)
        found["trace"] = _rel(project_root, newest)
    compile_log = test_dir / "compile.log"
    if compile_log.is_file():
        found["compile_log"] = _rel(project_root, compile_log)
    return found


def _read_randseed(scope_dir: Path) -> int | str | None:
    """First line of ``test.randseed`` — the seed the sim ran with.

    A one-line side-car, not a log: reading it is what lets an agent
    replay a failure (``rb randtest -r N``) without opening anything.
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
    """When this run happened, taken from files — never from the clock.

    The envelope's mtime is the moment the runner finished writing the
    result, which is exactly the "last status" timestamp. Without an
    envelope the newest listed artefact is the best available proxy and
    the entry says so via ``source``.
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
    """One run's overlay record."""
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
            # Only the three fields the overlay promises, in a fixed order,
            # and only when the envelope carries them: a project whose runs
            # predate the record gets no key at all, so a refresh with
            # nothing re-run stays byte-identical (#379's whole point). The
            # values come from the envelope, never from a clock here.
            block = {
                "duration_sec": compile_record.get("duration_sec"),
                "builder": compile_record.get("builder"),
                "reused": compile_record.get("reused"),
            }
            # An all-null block says nothing and is not the same as absent —
            # it is what a config whose prepare() failed in the build job
            # leaves behind, and the entry-level None filter below does not
            # reach nested values. Drop it rather than publish three nulls.
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
    """Run scopes under one test artefact dir, base run first.

    ``run-NNNN`` is the ``randtest`` layout (guidelines → Artifact
    Layout); a plain ``rb test`` writes straight into the test root.
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
      overlay (dict): the ``results-overlay.json`` payload.
      entries (dict): the ``tests`` block, keyed by test node id.
      problems (list[dict]): envelopes that could not be read.
      unmatched (list[str]): overlay ids with no node in the graph it was
        refreshed against (only populated when a graph was supplied).
      missing (list[str]): test nodes in that graph with no result at all.
      path (Path | None): where the overlay was written, once it has been.
      coverage (CoverageJoin | None): the coverage join, when one was
        attempted. ``None`` means coverage was not asked for.
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
        """The coverage block's summary, or ``None`` when there is none."""
        block = (self.coverage.block if self.coverage else None) or {}
        return block.get("summary")


def _declared_test_names(tests_yaml: str) -> dict[str, str]:
    """``sanitized artefact dir name -> declared test name`` for one suite.

    Read through :class:`~rtl_buddy.config.suite.SuiteConfig`, the same
    loader ``rb test`` uses, so the overlay's ids cannot disagree with
    the config tier's. Only needed for a directory whose envelope is
    missing — an envelope carries the real (possibly sweep-expanded)
    name itself.
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
    """The real test name behind a sanitized artefact directory.

    The envelope is authoritative — it records the name the runner ran,
    including a sweep expansion the suite config alone cannot reproduce.
    Falling back to the declared-name map un-sanitizes the common case;
    failing that the directory name stands in, which at least keeps the
    artefacts reachable.
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
) -> ResultsOverlay:
    """Scan every suite's artefacts and build the results overlay.

    Args:
      project_root: Directory holding ``root_config.yaml``. Every id and
        path in the overlay is relative to it.
      verif_dir: Tree searched for ``tests.yaml``. Defaults to
        ``<project_root>/verif`` — the config tier's default, so the two
        files describe the same set of suites.
      graph: An already-loaded ``graph.json``. Optional; when given, each
        entry is cross-checked against it (``in_graph``) and the tests it
        declares with no result at all are reported in ``missing``.
      coverage: Join the run's coverage in (#402/#390). ``True`` (or
        ``"auto"``) reads the newest ``cov_dir/manifest.json`` and falls
        back to the per-test raw databases this scan itself found;
        ``"model"`` reads the manifest only; any other string is a path
        to a merged LCOV ``.info`` to ingest; ``False`` skips the join.
        A tree with no coverage artefacts is not an error — the
        ``coverage`` block is simply absent, which is also what keeps an
        overlay written before this feature byte-identical.
      cov_dir / cov_manifest: read coverage from here rather than from
        the newest ``cov_dir/manifest.json`` under the project. Naming
        either makes a failure to read it a reported problem.

    Returns:
      ResultsOverlay: the payload plus the bookkeeping the CLI reports.

    Never raises for a broken envelope — it lands in ``problems`` and the
    rest of the overlay is still built.
    """
    root = Path(os.path.realpath(str(project_root)))
    search_verif = Path(verif_dir) if verif_dir is not None else root / "verif"

    entries: dict[str, dict] = {}
    problems: list[dict] = []

    for tests_yaml in _walk_yaml_files(str(search_verif), "tests.yaml"):
        suite_dir = Path(tests_yaml).parent
        suite_rel = _rel(root, suite_dir)
        artefact_root = suite_dir / "artefacts"
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
        # Only *simulation* tests. The config tier also emits a `test`
        # node per synthesis / formal / CDC / FPGA run (#376), and those
        # leave no `artefacts/<test>/result.json` behind — counting them
        # here would report every one of them `missing` and make
        # `rb graph results --strict` fail on any project that runs more
        # than one flow. A node with no `flow` at all predates the stamp
        # and is a simulation test by the same default the stamp uses.
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
        # Beside `artefacts.coverage` — the path to the raw database was
        # all an entry carried, and a path is not a number.
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
        # Placed before the (long) per-test block so a human opening the
        # file reads the verdict first. Filled in below; the key's
        # position is fixed by this insertion, not by that assignment.
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
    """Fold one test's run scopes into a single overlay entry.

    The entry's top level is the **last** run: the newest timestamp wins,
    tie-broken by the highest ``run_id``, so a ``randtest`` reports its
    latest iteration while every iteration stays listed under ``runs``.

    A scope that shows no evidence of a run — no envelope and not one
    recognized artefact — contributes nothing, and a directory whose
    every scope is empty is not a test at all. That positive test is what
    keeps another command's per-suite workspace (an `fpv.yaml` living
    beside a `tests.yaml`, say) out of the overlay: the deny-list in
    :func:`_is_test_dir` names the cases known today, this catches the
    rest. ``None`` means "not a test directory".
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


# ---------------------------------------------------------------------------
# Writing / loading — the join hooks `rb graph query` (#380) uses
# ---------------------------------------------------------------------------


def write_overlay(overlay: dict, path: str | os.PathLike) -> Path:
    """Write the overlay atomically, creating parent directories.

    Formatting is stable and every collection is sorted, so refreshing an
    overlay after a re-run diffs only where a result actually moved.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(overlay, ensure_ascii=True, indent=2) + "\n")
    os.replace(tmp, target)
    return target


def load_overlay(path: str | os.PathLike) -> dict | None:
    """Load an overlay, or ``None`` when there is none to load.

    Accepts the file itself, the directory holding it, or a project root
    (``<root>/artefacts/graph/results-overlay.json``), so a consumer that
    only knows where ``graph.json`` is can ask for the overlay beside it
    without rebuilding the path. Never raises: an absent, unreadable or
    foreign file all mean "no results known", which is a state every
    consumer has to handle anyway — the graph is queryable without an
    overlay.
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
    """The overlay entry for one node id, or ``None``.

    *The* join hook: the overlay is keyed by node id precisely so a
    consumer walking ``graph.json`` needs no name mangling, no suite
    lookup and no knowledge of the artefact layout to answer "what
    happened to this test". Non-test nodes simply have no entry.
    """
    if not overlay:
        return None
    return (overlay.get("tests") or {}).get(node_id)


def annotate_graph(graph: dict, overlay: dict | None) -> int:
    """Attach overlay entries to a graph's nodes **in memory**.

    Returns the number of nodes annotated. The caller's ``graph`` dict is
    mutated, never the file: ``graph.json`` on disk stays hash-stable
    across overlay refreshes, which is the whole reason the overlay is a
    separate file. Use this on a graph you just loaded and are about to
    query or render, and do not write the result back to ``graph.json``.
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
    """Which graph this overlay was refreshed against.

    The ``graph-meta.json`` fingerprint is carried so a consumer can tell
    an overlay refreshed against the current graph from one left behind
    by an older build — without it, a stale overlay and a fresh one are
    indistinguishable.
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
) -> ResultsOverlay:
    """Collect results and write ``results-overlay.json``.

    The one call behind ``rb graph results``. ``graph.json`` is read (for
    the id cross-check, the fingerprint linkage and the coverage join)
    and never written.
    """
    root = Path(os.path.realpath(str(project_root)))
    out = Path(out_dir) if out_dir is not None else default_graph_dir(root)
    graph_file = Path(graph_path) if graph_path is not None else out / GRAPH_JSON_NAME
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
    )
    linkage = {**graph_linkage(graph_file.parent), "path": _rel(root, graph_file)}
    # Rebuilt rather than assigned into, so the linkage sits with the
    # other header keys instead of trailing the per-test block, and the
    # two long blocks (coverage, tests) come last in reading order.
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
