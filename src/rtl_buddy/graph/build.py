# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""``rb graph build`` — assemble the merged design knowledge graph (#377).

Three tiers, one file. This module owns the orchestration:

1. **design** — ``rtl-buddy-view graph`` once per model, reusing ``rb
   hier``'s ``artefacts/hier/<model>/hier.f`` filelist machinery, plus
   once per **testbench** rooted at its ``toplevel:`` (``--tb-top``),
   reusing ``rb hier --view tb``'s DUT+TB filelist merge;
2. **config** — :func:`rtl_buddy.graph.extract_config_tier` over the
   ``specs.yaml`` / ``models.yaml`` / ``tests.yaml`` trees;
3. **binding** — Graphify's deterministic pass over verif Python and
   spec markdown, only when Graphify is installed and never with its
   LLM pass unless explicitly asked for.

The tiers are unioned by :func:`rtl_buddy.graph.merge.merge_graphs` and
written to ``artefacts/graph/graph.json`` with provenance beside it in
``graph-meta.json``.

One stage runs *after* that union: :func:`rtl_buddy.graph.binding.bind_python`
(#378), which ties each cocotb test to its Python module, that module to
the DUT ``module:`` node, and its ``dut.<name>`` accesses to ``port:``
nodes. It has to come last because it reads both halves of the merged
graph at once, so its output is a fourth graph that is merged in on a
second pass.

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
from ..config.test import TestConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..tools.hier_rtl_buddy_view import RtlBuddyViewGraph
from . import graphify as graphify_mod
from .binding import BINDING_TIER, bind_python, collect_sources
from .config_tier import (
    CONFIG_TIER,
    EXTRACTED,
    GRAPH_JSON_NAME,
    GRAPH_META_NAME,
    SCHEMA_VERSION,
    extract_config_tier,
    module_id,
    testbench_id,
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
#: TB-rooted exports nest under their DUT: ``design/<model>/tb/<tb>``.
#: Mirrors ``rb hier --view tb``'s ``artefacts/hier/<model>/tb/<tb>``
#: filelist cache, which is where their sources come from.
TB_SUBDIR = "tb"
BINDING_FILE = "binding/graph.json"

#: The in-process binding stage's own export (#378). Kept apart from
#: ``binding/graph.json`` because that file is Graphify's — the binding
#: *tier* has two producers and a merge surprise has to be traceable to
#: exactly one of them.
BIND_FILE = "bind/graph.json"


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
        testbenches = self.extra.get("testbenches")
        if testbenches is not None:
            block["testbenches"] = testbenches
        collisions = self.extra.get("id_collisions")
        if collisions:
            block["id_collisions"] = collisions
        return block

    def row_detail(self) -> str:
        """What the ``rb graph build`` summary table shows for this tier.

        A tier that did not run explains itself with ``detail``; one
        that did is described by what it covered, so the DUT and TB
        halves of the design tier are both visible without opening the
        meta sidecar. Failures are counted the same way whichever half
        they came from — they are per-item, and the tier is still built.
        """
        if self.detail:
            return self.detail
        parts = []
        models = self.extra.get("models")
        if models is not None:
            parts.append(f"{len(models)} model(s)")
        testbenches = self.extra.get("testbenches")
        if testbenches is not None:
            parts.append(f"{len(testbenches)} testbench(es)")
        collisions = self.extra.get("id_collisions")
        if collisions:
            parts.append(f"{len(collisions)} id(s) suite-qualified")
        if self.failures:
            parts.append(f"{len(self.failures)} failed")
        return ", ".join(parts) or "-"


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
      binding (dict): Post-merge binding-stage summary (#378).
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
    binding: dict = dc_field(default_factory=dict)
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
            "binding": self.binding,
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


def _model_key(model: ModelConfig) -> str:
    """Identity of a model across the two ways it can be reached.

    A model found by walking ``design/`` and the same model reached
    through a test's ``model_path:`` are the same thing; comparing the
    ``ModelConfig`` objects would say otherwise.
    """
    return f"{os.path.realpath(model.path)}#{model.name}"


# ---------------------------------------------------------------------------
# Testbench selection
# ---------------------------------------------------------------------------


@dataclass
class TestbenchTarget:
    """One TB-rooted design-tier export.

    Attributes:
      suite_rel (str): Repo-relative suite directory — the same string
        the config tier puts in ``tb:<suite dir>#<name>``.
      suite_dir (str): Absolute suite directory. Anchors the testbench
        filelist's relative entries, exactly as the compile flow does.
      tb_name (str): ``testbenches:`` entry name.
      tb_top (str): Module the export is rooted at — the testbench's
        ``toplevel:`` when declared, else its name (the project
        convention ``rb hier --view tb`` already relies on).
      model (ModelConfig): The DUT whose filelist the TB filelist is
        merged on top of.
      test (TestConfig): The first test that names this testbench.
        Carries the ``tb`` the exporter reads; nothing test-specific
        (plusargs, sweeps) affects an elaborated hierarchy.
    """

    suite_rel: str
    suite_dir: str
    tb_name: str
    tb_top: str
    model: ModelConfig
    test: TestConfig

    @property
    def node_id(self) -> str:
        """Config-tier ``tb:`` node this export belongs to."""
        return testbench_id(self.suite_rel, self.tb_name)

    @property
    def label(self) -> str:
        """``<suite dir>#<tb name>`` — how failures name this target."""
        return f"{self.suite_rel}#{self.tb_name}"


def testbenches_from_suites(
    project_root: str | os.PathLike,
    verif_dir: str | os.PathLike,
    models: list[ModelConfig] | None = None,
) -> list[TestbenchTarget]:
    """Every testbench worth a TB-rooted export, de-duplicated.

    Reads the same ``tests.yaml`` files the config tier reads, through
    the same :class:`~rtl_buddy.config.suite.SuiteConfig` loader, so the
    testbenches exported here are exactly the ones that got ``tb:``
    nodes. A suite that fails to load is skipped silently — the config
    tier already reports it, and reporting it twice would double-count
    the failure in the envelope.

    Two testbenches are the same export when they resolve to the same
    ``(model, suite dir, testbench filelist, tb top)``: that tuple is
    the entire input to the viewer, so a second invocation could only
    reproduce the first one's bytes. Names differing is not a
    difference.

    A testbench whose top is the DUT top is dropped: ``--tb-top
    <model.name>`` would re-elaborate exactly what the DUT export
    already covered. That is the cocotb/SystemC case, where
    ``toplevel:`` is required *and* names the DUT — there is no SV
    testbench above it to add.

    Args:
      project_root: Root that ``suite_rel`` is relative to.
      verif_dir: Tree walked for ``tests.yaml``.
      models: When given, only testbenches whose model is in this list
        are returned, so ``--model`` / ``--regression`` narrows the TB
        exports the same way it narrows the DUT exports. ``None`` means
        no filtering.

    Returns:
      list[TestbenchTarget]: sorted by ``(suite dir, testbench name)``.
    """
    from ..config.suite import SuiteConfig
    from ..tools.spec_trace import _walk_yaml_files

    verif = str(verif_dir)
    if not os.path.isdir(verif):
        return []
    allowed = {_model_key(m) for m in models} if models is not None else None

    seen: set[tuple] = set()
    targets: list[TestbenchTarget] = []
    for path in _walk_yaml_files(verif, "tests.yaml"):
        suite_dir = os.path.dirname(os.path.realpath(path))
        suite_rel = rel_path(project_root, suite_dir)
        try:
            suite = SuiteConfig(path)
        except FatalRtlBuddyError:
            continue
        for test in suite.get_tests():
            tb = test.get_testbench()
            model = test.get_model()
            if allowed is not None and _model_key(model) not in allowed:
                continue
            tb_top = tb.toplevel or tb.get_name()
            if tb_top == model.name:
                continue
            key = (
                suite_dir,
                _model_key(model),
                tuple(tb.get_filelist()),
                tb_top,
            )
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                TestbenchTarget(
                    suite_rel=suite_rel,
                    suite_dir=suite_dir,
                    tb_name=tb.get_name(),
                    tb_top=tb_top,
                    model=model,
                    test=test,
                )
            )
    return sorted(targets, key=lambda t: (t.suite_rel, t.tb_name))


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


def _tb_exporters(
    project_root: Path,
    targets: list[TestbenchTarget],
    out_dir: Path,
    *,
    view_executable: str,
    frontend: str | None,
) -> list[tuple[TestbenchTarget, RtlBuddyViewGraph]]:
    """One TB-rooted exporter per testbench, output paths disambiguated.

    ``design/<model>/tb/<tb>`` is unique in every project that does not
    name two testbenches in two suites identically *and* point them at
    the same model; when one does, the later ones get a ``-2``, ``-3``
    suffix rather than overwriting the first export.
    """
    exporters = []
    used: set[str] = set()
    for target in targets:
        base = f"{target.model.name}/{TB_SUBDIR}/{target.tb_name}"
        slug, index = base, 2
        while slug in used:
            slug, index = f"{base}-{index}", index + 1
        used.add(slug)
        exporter = RtlBuddyViewGraph(
            name=f"graph/design/{slug}",
            model_cfg=target.model,
            suite_dir=str(project_root),
            output=str(out_dir / DESIGN_SUBDIR / slug / GRAPH_JSON_NAME),
            project_root=str(project_root),
            frontend=frontend,
            executable=view_executable,
            test_cfg=target.test,
            test_suite_dir=target.suite_dir,
        )
        exporters.append((target, exporter))
    return exporters


def _tb_stitch_link(node_id: str, module_node_id: str) -> dict:
    """``tb:<suite>#<name> --maps_to--> module:<tb top>``.

    The same edge type the model node uses for its config↔design
    stitch: a ``tb:`` node is metadata about an elaboration, and
    ``maps_to`` is already "this config thing is that design thing".
    Emitting a new edge type for the identical relation would make
    every consumer learn two words for one idea.
    """
    return {
        "source": node_id,
        "target": module_node_id,
        "type": "maps_to",
        "confidence": EXTRACTED,
    }


def _run_tb_tier(
    project_root: Path,
    exporters: list[tuple[TestbenchTarget, RtlBuddyViewGraph]],
    report: TierReport,
) -> list[tuple[TestbenchTarget, dict]]:
    """Invoke the viewer per testbench; return the graphs that came back.

    Failures are recorded per testbench exactly as the model loop
    records them per model — one broken testbench costs its own
    hierarchy, never the tier.
    """
    pairs: list[tuple[TestbenchTarget, dict]] = []
    built: list[str] = []
    for target, exporter in exporters:
        try:
            rc = exporter.run()
        except FatalRtlBuddyError as exc:
            report.failures.append({"testbench": target.label, "error": str(exc)})
            continue
        if rc != 0:
            report.failures.append(
                {
                    "testbench": target.label,
                    "error": f"rtl-buddy-view graph exited {rc}",
                    "log": rel_path(project_root, exporter.log_path()),
                }
            )
            log_event(
                logger,
                logging.WARNING,
                "graph_build.tb_export_failed",
                testbench=target.label,
                tb_top=target.tb_top,
                returncode=rc,
                log=exporter.log_path(),
            )
            continue
        try:
            payload = json.loads(Path(exporter.output).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            report.failures.append({"testbench": target.label, "error": str(exc)})
            continue
        pairs.append((target, payload))
        built.append(target.label)
        if report.generator is None:
            report.generator = (payload.get("graph") or {}).get("generator")
    report.extra["testbenches"] = built
    return pairs


# ---------------------------------------------------------------------------
# Testbench id collisions
#
# `module:<name>` is a *global* id by contract, but a SystemVerilog module
# name is only unique inside one elaboration. Testbenches are where that
# stops being a technicality: the conventional name for a testbench top is
# `tb_top`, and a project with eight suites has eight different modules
# called that, in eight different files. Unioned naively they become one
# node that instantiates every DUT in the project, and one
# `inst:tb_top/tb_top.i_dut` that is `instance_of` four different modules.
# Those are not merge artefacts a consumer can filter out — they are
# statements the graph makes that are false.
#
# So a TB export's ids are qualified with the suite that owns them when,
# and only when, the same id is claimed by a different *file* somewhere
# else in the design tier. DUT ids are never touched: they are the weld,
# and the whole point of the TB export is that its `module:<dut>` is the
# same node the DUT export produced.
# ---------------------------------------------------------------------------

#: Separates a design-tier id from the suite that disambiguates it.
#: ``@`` cannot appear in a module name or an instance path, so a
#: qualified id is always decomposable and never collides with a real one.
QUALIFIER_SEP = "@"


def _ambiguous_ids(graphs: list[dict]) -> dict[str, list[str]]:
    """Design-tier ids claimed by more than one source file.

    Nodes without a ``file`` are ignored rather than guessed at: an id
    with no evidence of where it came from is not evidence of a clash.
    """
    files: dict[str, set[str]] = {}
    for graph in graphs:
        for node in graph.get("nodes") or []:
            node_id, file = node.get("id"), node.get("file")
            if node_id and file:
                files.setdefault(node_id, set()).add(file)
    return {k: sorted(v) for k, v in files.items() if len(v) > 1}


def _qualify_graph(
    graph: dict, qualifier: str, ambiguous: dict[str, list[str]]
) -> tuple[dict, dict[str, str]]:
    """Suite-qualify the ambiguous ids in one TB export.

    Two things get qualified:

    * any node whose id another file also claims — the collision itself;
    * every ``inst:`` node in the export, when its **root module** is one
      of those. An instance id embeds the root it was reached from
      (``inst:tb_top/tb_top.i_dut``), so if the root is ambiguous the
      whole path is, including the parts that happen not to clash today.

    Returns the rewritten graph (the on-disk per-testbench export is left
    exactly as the viewer wrote it — qualification is a merge-time
    decision, not a fact about the elaboration) and the id map that was
    applied.
    """
    design = (graph.get("graph") or {}).get("design") or {}
    root_id = module_id(design.get("top") or "")
    rename: dict[str, str] = {}
    for node in graph.get("nodes") or []:
        node_id = node.get("id")
        if node_id and node_id in ambiguous:
            rename[node_id] = f"{node_id}{QUALIFIER_SEP}{qualifier}"
    if root_id in ambiguous:
        for node in graph.get("nodes") or []:
            node_id = node.get("id")
            if node_id and node_id.startswith("inst:"):
                rename.setdefault(node_id, f"{node_id}{QUALIFIER_SEP}{qualifier}")
    if not rename:
        return graph, {}

    nodes = []
    for node in graph.get("nodes") or []:
        node_id = node.get("id")
        if node_id in rename:
            node = dict(node)
            node["id"] = rename[node_id]
            # Keep the name the design actually uses reachable: `label`
            # is what `rb graph query` matches on, and an agent asking
            # about `tb_top` must still find every one of them.
            node["unqualified_id"] = node_id
            node["qualified_by"] = qualifier
        nodes.append(node)
    links = []
    for link in graph.get("links") or []:
        source, target = link.get("source"), link.get("target")
        if source in rename or target in rename:
            link = dict(link)
            link["source"] = rename.get(source, source)
            link["target"] = rename.get(target, target)
        links.append(link)
    qualified = dict(graph)
    qualified["nodes"] = nodes
    qualified["links"] = links
    return qualified, rename


def _qualify_tb_graphs(
    model_graphs: list[dict],
    pairs: list[tuple[TestbenchTarget, dict]],
    report: TierReport,
) -> tuple[list[dict], list[dict]]:
    """Resolve TB id collisions and emit the ``tb:`` -> ``module:`` stitches.

    The stitch points at the top the viewer *actually* elaborated
    (``graph.design.top``, which it auto-corrects when the ``--tb-top``
    hint names no real module), after qualification — so the edge always
    lands on a node that exists and is the right one.
    """
    ambiguous = _ambiguous_ids(model_graphs + [graph for _, graph in pairs])
    graphs: list[dict] = []
    stitches: list[dict] = []
    collisions: dict[str, dict] = {}
    for target, graph in pairs:
        qualified, rename = _qualify_graph(graph, target.suite_rel, ambiguous)
        graphs.append(qualified)
        design = (graph.get("graph") or {}).get("design") or {}
        root_id = module_id(design.get("top") or design.get("tb_top") or target.tb_top)
        stitches.append(_tb_stitch_link(target.node_id, rename.get(root_id, root_id)))
        for original, new_id in rename.items():
            entry = collisions.setdefault(
                original,
                {"id": original, "files": ambiguous.get(original, []), "qualified": []},
            )
            entry["qualified"].append(new_id)
    if collisions:
        report.extra["id_collisions"] = [collisions[k] for k in sorted(collisions)]
        log_event(
            logger,
            logging.WARNING,
            "graph_build.tb_id_collision",
            ids=len(collisions),
            example=sorted(collisions)[0],
        )
    return graphs, stitches


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
    tb: bool = True,
    bind: bool = True,
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
      tb: False skips the TB-rooted half of the design tier — DUT
        hierarchies only, no SV testbench modules or instances. It is a
        cost switch, not a correctness one: every testbench doubles the
        elaboration work for the design it sits on top of.
      bind: False skips the post-merge binding stage (#378) — no
        ``binds_to`` / ``drives`` / ``checks_against`` edges.
      graphify_enabled: False skips Graphify's binding tier without probing.
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
    tb_exporters: list[tuple[TestbenchTarget, RtlBuddyViewGraph]] = []
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
            # TB-rooted exports are part of this tier: same exporter,
            # same filelist machinery, one extra `--tb-top`. Their
            # sources join the tier's input hashes, which is what keeps
            # the no-op check honest when only a testbench changed.
            if tb:
                for target, exporter in _tb_exporters(
                    root,
                    testbenches_from_suites(root, search_verif, models),
                    out,
                    view_executable=view_executable,
                    frontend=frontend,
                ):
                    try:
                        exporter.write_filelist()
                    except Exception as exc:  # FilelistError and friends
                        design_report.failures.append(
                            {"testbench": target.label, "error": str(exc)}
                        )
                        continue
                    sources.extend(exporter.source_files())
                    tb_exporters.append((target, exporter))
            design_report.inputs = hash_inputs(root, sources)
            if not exporters and not tb_exporters:
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
        binding_report.extra["llm_pass"] = graphify_llm
        if not graphify_inputs:
            binding_report.status = SKIPPED
            binding_report.detail = "no verif Python or spec markdown found"
    # The in-process binding stage reads verif/spec Python whether or not
    # Graphify is installed, so its inputs belong in the tier's hash list
    # unconditionally: editing a cocotb test must invalidate the cache.
    bind_inputs = collect_sources(search_verif, search_spec) if bind else []
    binding_report.inputs = hash_inputs(
        root, sorted(set(graphify_inputs + bind_inputs))
    )
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
            binding=stored_meta.get("binding", {}),
        )

    # --- run the exporters ----------------------------------------------
    tier_graphs: list[tuple[str, dict]] = []
    tier_files: list[str] = []

    tb_stitches: list[dict] = []
    if exporters or tb_exporters:
        design_graphs = _run_design_tier(root, exporters, design_report)
        if tb_exporters:
            tb_pairs = _run_tb_tier(root, tb_exporters, design_report)
            tb_graphs, tb_stitches = _qualify_tb_graphs(
                design_graphs, tb_pairs, design_report
            )
            design_graphs += tb_graphs
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
                str(e.output)
                for _, e in [*exporters, *tb_exporters]
                if Path(e.output).is_file()
            )
        else:
            design_report.status = FAILED
            design_report.detail = "no model exported successfully"

    # The `tb:` node belongs to the config tier, so its stitch to the
    # hierarchy it elaborates is a config-tier link — the same asymmetry
    # `model --maps_to--> module:` already has. It can only be written
    # after the export, because only the export knows the top the viewer
    # really elaborated (a testbench may declare no `toplevel:` at all)
    # and whether that id had to be suite-qualified. Where both exist the
    # export wins: the config tier's `toplevel:`-derived edge is a
    # declaration, this one is an observation of the same thing.
    if tb_stitches:
        exported = {link["source"] for link in tb_stitches}
        config.graph["links"] = [
            link
            for link in config.graph["links"]
            if not (link["source"] in exported and link["type"] == "maps_to")
        ]
        config.graph["links"].extend(tb_stitches)
        # Restore the extractor's canonical ordering so the config
        # tier's own file stays byte-identical across runs that changed
        # nothing.
        config.graph["links"].sort(
            key=lambda link: (link["source"], link["target"], link["type"])
        )
        config_report.links = len(config.graph["links"])

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

    # --- merge -----------------------------------------------------------
    merge_kwargs = {
        "generator": {"tool": "rtl_buddy", "version": _rtl_buddy_version()},
        "schema_version": SCHEMA_VERSION,
        "project_root_rel": _project_root_rel(root, graph_path),
    }
    merged = merge_graphs(tier_graphs, **merge_kwargs)

    # --- binding stage (post-merge, #378) --------------------------------
    #
    # It runs *after* the union because it needs both halves at once: the
    # config tier says which cocotb module belongs to which toplevel, the
    # design tier owns the `port:` nodes a `dut.<name>` access resolves
    # against. Its output is a fourth graph, re-merged in below, so the
    # stage itself stays a pure function of the graph it was handed.
    binding_info: dict = {"status": SKIPPED, "detail": "disabled (--no-bind)"}
    if bind:
        stage = bind_python(
            merged,
            root,
            generator={
                "tool": "rtl_buddy",
                "version": _rtl_buddy_version(),
                "tier": BINDING_TIER,
                "stage": "bind",
            },
        )
        binding_info = stage.summary()
        if stage.links:
            bind_file = out / BIND_FILE
            write_graph_json(stage.graph, bind_file)
            tier_files.append(str(bind_file))
            tier_graphs.append((BINDING_TIER, stage.graph))
            merged = merge_graphs(tier_graphs, **merge_kwargs)
            log_event(
                logger,
                logging.INFO,
                "graph_build.bound",
                tests=stage.tests,
                drives=stage.drives,
                inferred=stage.inferred,
                checks=stage.checks,
            )

    merge_info: dict = {
        "strategy": "node-id-union",
        # De-duplicated: the binding tier has two producers (Graphify and
        # the post-merge stage) and contributes two graphs, but it is
        # still one tier.
        "tiers": list(dict.fromkeys(tier for tier, _ in tier_graphs)),
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
        "binding": binding_info,
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
        binding=binding_info,
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
        for key in ("models", "testbenches"):
            if key in stored and key not in report.extra:
                report.extra[key] = stored[key]


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
