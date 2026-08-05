# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""``rb graph build`` — assemble the merged design knowledge graph (#377).

Three tiers, one file. This module owns the orchestration:

1. **design** — ``rtl-buddy-view graph`` once per model, reusing ``rb
   hier``'s ``artefacts/hier/<model>/hier.f`` filelist machinery;
2. **config** — :func:`rtl_buddy.graph.extract_config_tier` over the
   ``specs.yaml`` / ``models.yaml`` / ``tests.yaml`` trees;
3. **binding** — Graphify's deterministic pass over verif Python and
   spec markdown, only when Graphify is installed and never with its
   LLM pass unless explicitly asked for.

The tiers are unioned by :func:`rtl_buddy.graph.merge.merge_graphs` and
written to ``artefacts/graph/graph.json`` with provenance beside it in
``graph-meta.json``.

Two properties are load-bearing:

* **Optional tiers stay optional.** A missing Graphify, an
  unexportable model, an unloadable suite — each is recorded in the
  meta sidecar and the envelope, and the graph is still written. Only
  ``--strict`` turns those into a non-zero exit.
* **A re-run with nothing changed is a no-op.** Every input is hashed
  before any exporter runs, and the combined fingerprint (inputs +
  tool versions + schema version) is compared against the one in
  ``graph-meta.json``. Matching fingerprint plus an existing
  ``graph.json`` means the build is skipped outright, which is why the
  cheap filelist generation happens before the expensive parse.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field as dc_field
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

from ..config.model import ModelConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..tools.hier_rtl_buddy_view import RtlBuddyViewGraph
from . import graphify as graphify_mod
from .config_tier import (
    CONFIG_TIER,
    GRAPH_JSON_NAME,
    GRAPH_META_NAME,
    SCHEMA_VERSION,
    extract_config_tier,
    write_graph_json,
    write_graph_meta,
)
from .merge import (
    dangling_targets,
    fingerprint,
    hash_inputs,
    merge_graphs,
    rel_path,
    stitch_points,
    tier_sort_key,
)

logger = logging.getLogger(__name__)

DESIGN_TIER = "design"
BINDING_TIER = "binding"

#: Tier statuses. ``pending`` is internal — a tier that got as far as
#: hashing its inputs but has not run yet. It resolves to ``built`` /
#: ``failed`` once the exporter runs, or to ``cached`` when the
#: fingerprint check short-circuits the build.
PENDING = "pending"
BUILT = "built"
CACHED = "cached"
SKIPPED = "skipped"
FAILED = "failed"

#: First ``rtl-buddy-view`` release carrying the ``graph`` subcommand
#: (rtl-buddy-view#126). Mirrors the ``0.3.0`` floor that ``rb
#: hier-query`` gates on in ``tool_manifest.py`` — the manifest floor
#: is what every view-backed command shares, and this is the extra
#: per-feature floor layered on top of it.
VIEW_GRAPH_MIN_VERSION = "0.4.0"

#: Where each tier's own export lands under ``artefacts/graph/``. Kept
#: on disk (not just in memory) so a failed merge is debuggable and so
#: ``graphify merge-graphs`` has real files to cross-check against.
DESIGN_SUBDIR = "design"
BINDING_FILE = "binding/graph.json"


def _rtl_buddy_version() -> str:
    try:
        return _pkg_version("rtl-buddy")
    except PackageNotFoundError:  # pragma: no cover - only in odd installs
        return "0+unknown"


def _version_tuple(value: str) -> tuple[int, ...]:
    """Leading (major, minor, patch) ints of a PEP 440 version string."""
    parts = []
    for segment in value.split(".")[:3]:
        match = re.match(r"\d+", segment)
        parts.append(int(match.group()) if match else 0)
    return tuple(parts)


def _is_dev_build(value: str) -> bool:
    """True for an untagged build (``0.3.1.dev1+g<sha>``, ``…+local``).

    An editable install off a feature branch legitimately carries a
    feature before the release that names it exists, so a dev build is
    trusted over the floor: the alternative is that nobody can use
    ``rb graph build`` until the view is tagged.
    """
    return ".dev" in value or "+" in value


def check_view_supports_graph(view_version: str | None) -> str | None:
    """Reason the installed viewer can't export the design tier, or None.

    ``None`` version means the probe failed (an old build without
    ``--version``); the subsequent invocation's exit code is then the
    real gate, so don't pre-emptively refuse.
    """
    if view_version is None or _is_dev_build(view_version):
        return None
    if _version_tuple(view_version) < _version_tuple(VIEW_GRAPH_MIN_VERSION):
        return (
            f"rtl-buddy-view {view_version} has no `graph` subcommand; "
            f"the design tier needs >= {VIEW_GRAPH_MIN_VERSION} "
            f'(pip install -U "rtl-buddy-view >= {VIEW_GRAPH_MIN_VERSION}")'
        )
    return None


@dataclass
class TierReport:
    """One tier's contribution to the build.

    Attributes:
      tier (str): ``design`` / ``config`` / ``binding``.
      status (str): ``built``, ``skipped`` or ``failed``.
      detail (str | None): Why, when not ``built``.
      inputs (list[dict]): ``{"path", "sha256"}`` of everything read.
      nodes (int): Nodes contributed (before the union).
      links (int): Links contributed (before the union).
      generator (dict | None): The tier's own ``graph.generator`` block.
      failures (list): Per-item failures that did not sink the tier.
      extra (dict): Tier-specific fields for the meta sidecar.
    """

    tier: str
    status: str = PENDING
    detail: str | None = None
    inputs: list[dict] = dc_field(default_factory=list)
    nodes: int = 0
    links: int = 0
    generator: dict | None = None
    failures: list = dc_field(default_factory=list)
    extra: dict = dc_field(default_factory=dict)

    def as_meta(self) -> dict:
        block: dict = {"status": self.status}
        if self.detail:
            block["detail"] = self.detail
        if self.generator:
            block["generator"] = self.generator
        block.update(self.extra)
        block["nodes"] = self.nodes
        block["links"] = self.links
        block["inputs"] = self.inputs
        if self.failures:
            block["failures"] = self.failures
        return block

    def as_payload(self) -> dict:
        block: dict = {
            "tier": self.tier,
            "status": self.status,
            "nodes": self.nodes,
            "links": self.links,
        }
        if self.detail:
            block["detail"] = self.detail
        if self.failures:
            block["failures"] = self.failures
        models = self.extra.get("models")
        if models is not None:
            block["models"] = models
        return block


@dataclass
class GraphBuild:
    """Result of one ``rb graph build``.

    Attributes:
      graph_path (Path): Written (or existing, when ``unchanged``)
        ``graph.json``.
      meta_path (Path): The ``graph-meta.json`` sidecar.
      unchanged (bool): True when the fingerprint matched and nothing ran.
      tiers (list[TierReport]): Per-tier outcome.
      nodes (int): Nodes in the merged graph.
      links (int): Links in the merged graph.
      fingerprint (str): Combined input + tool-version hash.
      merge (dict): Merge bookkeeping (strategy, stitch points, cross-check).
      graph (dict | None): The merged payload (None when ``unchanged``).
    """

    graph_path: Path
    meta_path: Path
    unchanged: bool
    tiers: list[TierReport]
    nodes: int = 0
    links: int = 0
    fingerprint: str = ""
    merge: dict = dc_field(default_factory=dict)
    graph: dict | None = None

    def failed_tiers(self) -> list[TierReport]:
        return [t for t in self.tiers if t.status == FAILED]

    def has_failures(self) -> bool:
        return bool(self.failed_tiers()) or any(t.failures for t in self.tiers)

    def payload(self, project_root: Path) -> dict:
        return {
            "graph": rel_path(project_root, self.graph_path),
            "meta": rel_path(project_root, self.meta_path),
            "unchanged": self.unchanged,
            "nodes": self.nodes,
            "links": self.links,
            "fingerprint": self.fingerprint,
            "tiers": [t.as_payload() for t in self.tiers],
            "merge": self.merge,
        }


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------


def models_from_regression(reg_path: str | os.PathLike) -> list[ModelConfig]:
    """Every model referenced by the suites a regression config lists.

    Goes through :class:`~rtl_buddy.config.reg.RegConfig`, so the graph
    covers exactly what ``rb regression -c <file>`` would run — not a
    parallel notion of "the models in this project".
    """
    from ..config.reg import RegConfig

    reg = RegConfig(name="graph/regression", path=str(reg_path))
    seen: dict[str, ModelConfig] = {}
    for suite in reg.get_suite_configs():
        for test in suite.get_tests():
            model = test.get_model()
            key = f"{os.path.realpath(model.path)}#{model.name}"
            seen.setdefault(key, model)
    return [seen[k] for k in sorted(seen)]


def models_from_design_tree(design_dir: str | os.PathLike) -> list[ModelConfig]:
    """Every model declared by a ``models.yaml`` under ``design_dir``.

    The default selection: the config tier already covers this whole
    tree, so exporting the same set keeps the two tiers talking about
    the same design instead of a subset of it.
    """
    from ..tools.spec_trace import discover_model_configs

    if not os.path.isdir(design_dir):
        return []
    return [model for _, model in discover_model_configs(str(design_dir))]


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------


def _design_exporters(
    project_root: Path,
    models: list[ModelConfig],
    out_dir: Path,
    *,
    view_executable: str,
    frontend: str | None,
) -> list[tuple[ModelConfig, RtlBuddyViewGraph]]:
    """One exporter per model, with its filelist already written."""
    exporters = []
    for model in models:
        target = out_dir / DESIGN_SUBDIR / model.name / GRAPH_JSON_NAME
        exporter = RtlBuddyViewGraph(
            name=f"graph/design/{model.name}",
            model_cfg=model,
            suite_dir=str(project_root),
            output=str(target),
            project_root=str(project_root),
            frontend=frontend,
            executable=view_executable,
        )
        exporters.append((model, exporter))
    return exporters


def _run_design_tier(
    project_root: Path,
    exporters: list[tuple[ModelConfig, RtlBuddyViewGraph]],
    report: TierReport,
) -> list[dict]:
    """Invoke the viewer per model; return the graphs that came back."""
    graphs: list[dict] = []
    built: list[str] = []
    for model, exporter in exporters:
        try:
            rc = exporter.run()
        except FatalRtlBuddyError as exc:
            report.failures.append({"model": model.name, "error": str(exc)})
            continue
        if rc != 0:
            report.failures.append(
                {
                    "model": model.name,
                    "error": f"rtl-buddy-view graph exited {rc}",
                    "log": rel_path(project_root, exporter.log_path()),
                }
            )
            log_event(
                logger,
                logging.WARNING,
                "graph_build.design_export_failed",
                model=model.name,
                returncode=rc,
                log=exporter.log_path(),
            )
            continue
        try:
            payload = json.loads(Path(exporter.output).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            report.failures.append({"model": model.name, "error": str(exc)})
            continue
        graphs.append(payload)
        built.append(model.name)
        if report.generator is None:
            report.generator = (payload.get("graph") or {}).get("generator")
    report.extra["models"] = built
    return graphs


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_graph(
    project_root: str | os.PathLike,
    *,
    models: list[ModelConfig] | None = None,
    spec_dir: str | os.PathLike | None = None,
    verif_dir: str | os.PathLike | None = None,
    design_dir: str | os.PathLike | None = None,
    out_dir: str | os.PathLike | None = None,
    view_executable: str = "rtl-buddy-view",
    view_version: str | None = None,
    frontend: str | None = None,
    design: bool = True,
    graphify_enabled: bool = True,
    graphify_llm: bool = False,
    graphify_executable: str = graphify_mod.GRAPHIFY_TOOL,
    graphify_cross_check: bool = True,
    graphify_version: str | None = None,
    force: bool = False,
) -> GraphBuild:
    """Build (or refresh) the merged graph under ``artefacts/graph``.

    Args:
      project_root: Directory holding ``root_config.yaml``. Node ids and
        every path in the meta sidecar are relative to it.
      models: Models to export in the design tier. ``None`` means every
        model under ``design_dir``; an empty list means none.
      spec_dir / verif_dir / design_dir: Search-root overrides, matching
        ``extract_config_tier``'s defaults.
      out_dir: Output directory. Defaults to ``<root>/artefacts/graph``.
      view_executable / view_version: The ``rtl-buddy-view`` binary and
        its probed version (used for the feature gate and fingerprint).
      design: False skips the design tier entirely (config-only graph).
      graphify_enabled: False skips the binding tier without probing.
      graphify_llm: Opt into Graphify's LLM pass. Off by default.
      graphify_cross_check: Run ``graphify merge-graphs`` and compare
        against the internal union.
      force: Rebuild even when the fingerprint is unchanged.

    Returns:
      GraphBuild: paths, per-tier reports, and whether anything ran.

    Never raises for a tier that could not be built — inspect
    ``failed_tiers()`` / ``has_failures()``. Only a genuinely
    unrecoverable setup problem (unreadable regression config, unwritable
    output directory) propagates.
    """
    root = Path(os.path.realpath(str(project_root)))
    search_spec = Path(spec_dir) if spec_dir is not None else root / "spec"
    search_verif = Path(verif_dir) if verif_dir is not None else root / "verif"
    search_design = Path(design_dir) if design_dir is not None else root / "design"
    out = Path(out_dir) if out_dir is not None else root / "artefacts" / "graph"
    graph_path = out / GRAPH_JSON_NAME
    meta_path = out / GRAPH_META_NAME

    if models is None:
        models = models_from_design_tree(search_design) if design else []
    if not design:
        models = []

    reports: dict[str, TierReport] = {}
    tools: dict[str, str | None] = {"rtl-buddy": _rtl_buddy_version()}

    # --- design tier: filelists first, so hashing precedes parsing ------
    exporters: list[tuple[ModelConfig, RtlBuddyViewGraph]] = []
    design_report = TierReport(tier=DESIGN_TIER)
    if not design:
        design_report.status = SKIPPED
        design_report.detail = "disabled (--no-design)"
    elif not models:
        design_report.status = SKIPPED
        design_report.detail = f"no models found under {rel_path(root, search_design)}"
    else:
        gate = check_view_supports_graph(view_version)
        if gate is not None:
            design_report.status = FAILED
            design_report.detail = gate
        else:
            tools["rtl-buddy-view"] = view_version
            sources: list[str] = []
            for model, exporter in _design_exporters(
                root,
                models,
                out,
                view_executable=view_executable,
                frontend=frontend,
            ):
                try:
                    exporter.write_filelist()
                except Exception as exc:  # FilelistError and friends
                    design_report.failures.append(
                        {"model": model.name, "error": str(exc)}
                    )
                    continue
                sources.extend(exporter.source_files())
                exporters.append((model, exporter))
            design_report.inputs = hash_inputs(root, sources)
            if not exporters:
                design_report.status = FAILED
                design_report.detail = "no model produced a filelist"
    reports[DESIGN_TIER] = design_report

    # --- config tier ----------------------------------------------------
    config = extract_config_tier(
        root,
        spec_dir=str(search_spec),
        verif_dir=str(search_verif),
        design_dir=str(search_design),
    )
    config_meta = (config.meta.get("tiers") or {}).get(CONFIG_TIER, {})
    config_report = TierReport(
        tier=CONFIG_TIER,
        status=BUILT,
        inputs=config_meta.get("inputs", []),
        nodes=len(config.graph["nodes"]),
        links=len(config.graph["links"]),
        generator=config_meta.get("generator"),
        failures=[
            {"suite": path, "error": "failed to load"}
            for path in config.suite_load_failures
        ],
    )
    reports[CONFIG_TIER] = config_report

    # --- binding tier (Graphify, optional) ------------------------------
    binding_report = TierReport(tier=BINDING_TIER)
    graphify_inputs: list[str] = []
    if not graphify_enabled:
        binding_report.status = SKIPPED
        binding_report.detail = "disabled (--no-graphify)"
    elif graphify_version is None:
        binding_report.status = SKIPPED
        binding_report.detail = (
            "graphify not installed — design + config tiers only "
            "(run `rb tool-check --explain graphify`)"
        )
    else:
        tools[graphify_mod.GRAPHIFY_TOOL] = graphify_version
        graphify_inputs = graphify_mod.collect_inputs(search_verif, search_spec)
        binding_report.inputs = hash_inputs(root, graphify_inputs)
        binding_report.extra["llm_pass"] = graphify_llm
        if not graphify_inputs:
            binding_report.status = SKIPPED
            binding_report.detail = "no verif Python or spec markdown found"
    reports[BINDING_TIER] = binding_report

    # --- no-op check ----------------------------------------------------
    tier_inputs = {name: report.inputs for name, report in reports.items()}
    fp = fingerprint(
        schema_version=SCHEMA_VERSION, tools=tools, tier_inputs=tier_inputs
    )
    stored_meta = _read_json(meta_path) or {}
    if not force and graph_path.is_file() and stored_meta.get("fingerprint") == fp:
        log_event(
            logger,
            logging.INFO,
            "graph_build.unchanged",
            graph=str(graph_path),
            fingerprint=fp,
        )
        existing = _read_json(graph_path) or {}
        _hydrate_from_meta(reports, stored_meta)
        return GraphBuild(
            graph_path=graph_path,
            meta_path=meta_path,
            unchanged=True,
            tiers=_ordered(reports),
            nodes=len(existing.get("nodes") or []),
            links=len(existing.get("links") or []),
            fingerprint=fp,
            merge=stored_meta.get("merge", {}),
        )

    # --- run the exporters ----------------------------------------------
    tier_graphs: list[tuple[str, dict]] = []
    tier_files: list[str] = []

    if exporters:
        design_graphs = _run_design_tier(root, exporters, design_report)
        if design_graphs:
            design_report.status = BUILT
            # One model is the common case; keep its envelope untouched
            # rather than wrapping it in a merged-of-one.
            design_graph = (
                design_graphs[0]
                if len(design_graphs) == 1
                else merge_graphs(
                    [(DESIGN_TIER, g) for g in design_graphs],
                    generator=design_report.generator
                    or {"tool": "rtl-buddy-view", "version": view_version or "unknown"},
                    schema_version=SCHEMA_VERSION,
                    project_root_rel="../..",
                )
            )
            design_report.nodes = len(design_graph.get("nodes") or [])
            design_report.links = len(design_graph.get("links") or [])
            tier_graphs.append((DESIGN_TIER, design_graph))
            tier_files.extend(
                str(e.output) for _, e in exporters if Path(e.output).is_file()
            )
        else:
            design_report.status = FAILED
            design_report.detail = "no model exported successfully"

    tier_graphs.append((CONFIG_TIER, config.graph))
    config_file = out / CONFIG_TIER / GRAPH_JSON_NAME
    write_graph_json(config.graph, config_file)
    tier_files.append(str(config_file))

    if binding_report.status == PENDING:
        binding_file = out / BINDING_FILE
        binding_file.parent.mkdir(parents=True, exist_ok=True)
        result = graphify_mod.run_extract(
            graphify_inputs,
            binding_file,
            executable=graphify_executable,
            llm=graphify_llm,
            log_path=out / "graphify.log",
            cwd=str(root),
        )
        if result.ok and result.graph is not None:
            binding_report.status = BUILT
            binding_report.nodes = len(result.graph.get("nodes") or [])
            binding_report.links = len(result.graph.get("links") or [])
            binding_report.generator = (result.graph.get("graph") or {}).get(
                "generator"
            )
            tier_graphs.append((BINDING_TIER, result.graph))
            tier_files.append(str(binding_file))
        else:
            binding_report.status = FAILED
            binding_report.detail = result.detail
            log_event(
                logger,
                logging.WARNING,
                "graph_build.graphify_failed",
                detail=result.detail,
            )

    # --- merge + write ---------------------------------------------------
    merged = merge_graphs(
        tier_graphs,
        generator={"tool": "rtl_buddy", "version": _rtl_buddy_version()},
        schema_version=SCHEMA_VERSION,
        project_root_rel=_project_root_rel(root, graph_path),
    )
    merge_info: dict = {
        "strategy": "node-id-union",
        "tiers": [tier for tier, _ in tier_graphs],
        "stitch_points": len(stitch_points(tier_graphs)),
        "dangling": dangling_targets(merged),
    }
    if graphify_version is not None and graphify_cross_check:
        merge_info["graphify_cross_check"] = graphify_mod.run_merge_cross_check(
            tier_files,
            out / "graphify-merged.json",
            internal=merged,
            executable=graphify_executable,
            log_path=out / "graphify.log",
            cwd=str(root),
        )
    else:
        merge_info["graphify_cross_check"] = {
            "status": "skipped",
            "detail": "graphify not installed"
            if graphify_version is None
            else "disabled",
        }

    meta = {
        "schema_version": SCHEMA_VERSION,
        "generated_by": {
            "tool": "rtl_buddy",
            "version": _rtl_buddy_version(),
            "command": "graph build",
        },
        "fingerprint": fp,
        "tools": tools,
        "merge": merge_info,
        "tiers": {name: report.as_meta() for name, report in reports.items()},
    }

    write_graph_json(merged, graph_path)
    write_graph_meta(meta, meta_path)
    log_event(
        logger,
        logging.INFO,
        "graph_build.written",
        graph=str(graph_path),
        nodes=len(merged["nodes"]),
        links=len(merged["links"]),
        tiers=",".join(tier for tier, _ in tier_graphs),
    )

    return GraphBuild(
        graph_path=graph_path,
        meta_path=meta_path,
        unchanged=False,
        tiers=_ordered(reports),
        nodes=len(merged["nodes"]),
        links=len(merged["links"]),
        fingerprint=fp,
        merge=merge_info,
        graph=merged,
    )


def _ordered(reports: dict[str, TierReport]) -> list[TierReport]:
    return [reports[k] for k in sorted(reports, key=tier_sort_key)]


def _hydrate_from_meta(reports: dict[str, TierReport], stored_meta: dict) -> None:
    """Re-state a skipped build's tiers from the sidecar it is reusing.

    Nothing ran, so the live reports only know what could be worked out
    before the exporters would have been invoked: the config tier has
    real counts (it is extracted to compute the fingerprint), the design
    tier has none. Filling both in from ``graph-meta.json`` keeps the
    envelope truthful about the ``graph.json`` on disk.

    A tier that **failed** in the cached build stays failed — the
    fingerprint matching only proves the inputs didn't move, and
    reporting a still-broken tier as ``cached`` would turn a permanent
    failure (viewer missing, model unparseable) into a green exit on
    every run after the first.
    """
    stored_tiers = stored_meta.get("tiers") or {}
    for name, report in reports.items():
        if report.status in (SKIPPED, FAILED):
            continue  # decided by this invocation's flags, not the cache
        stored = stored_tiers.get(name) or {}
        if stored.get("status") == FAILED:
            report.status = FAILED
            report.detail = stored.get("detail") or report.detail
            report.failures = stored.get("failures") or report.failures
            continue
        report.status = CACHED
        if not report.nodes:
            report.nodes = int(stored.get("nodes") or 0)
        if not report.links:
            report.links = int(stored.get("links") or 0)
        if not report.failures:
            report.failures = stored.get("failures") or []
        if "models" in stored and "models" not in report.extra:
            report.extra["models"] = stored["models"]


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _project_root_rel(project_root: Path, graph_path: Path) -> str:
    """Project root relative to the merged graph file.

    Node ``file`` fields are project-relative, so a consumer holding only
    ``graph.json`` gets back to the sources with
    ``dirname(graph.json)/project_root_rel/<node file>``. The contracted
    ``artefacts/graph/graph.json`` yields ``"../.."``.
    """
    try:
        return Path(
            os.path.relpath(project_root, graph_path.resolve().parent)
        ).as_posix()
    except ValueError:  # pragma: no cover - different drives on Windows
        return project_root.as_posix()
