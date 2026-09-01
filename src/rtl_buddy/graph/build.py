# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""``rb graph build`` — assemble the merged design knowledge graph (#377).

Three tiers, one file. This module owns the orchestration:

1. **design** — ``rtl-buddy-view graph`` once per model, reusing ``rb
   hier``'s ``artefacts/hier/<model>/hier.f`` filelist machinery, plus
   once per **testbench** rooted at its ``toplevel:`` (``--tb-top``),
   reusing ``rb hier --view tb``'s DUT+TB filelist merge, plus once per
   **flow run** whose ``top:`` only elaborates inside the flow's own
   filelist (#385 — an fpv checker top over the model + ``properties:``);
2. **config** — :func:`rtl_buddy.graph.extract_config_tier` over the
   ``specs.yaml`` / ``models.yaml`` / ``tests.yaml`` trees;
3. **binding** — the extractor's (``rb-graph-extract``) deterministic
   pass over verif Python and spec markdown, only when the tool is
   installed.

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

* **Optional tiers stay optional.** A missing extractor, an
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
import shutil
from dataclasses import dataclass, field as dc_field
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

from ..config.model import ModelConfig
from ..config.test import TestConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..tools.hier_rtl_buddy_view import RtlBuddyViewGraph
from . import extract as extract_mod
from .binding import BINDING_TIER, bind_python, collect_sources
from .config_tier import (
    CONFIG_TIER,
    ELABORATES_AS,
    EXTRACTED,
    FLOW_FPV,
    GRAPH_JSON_NAME,
    GRAPH_META_NAME,
    SCHEMA_VERSION,
    TARGETS,
    _collect_flows,
    extract_config_tier,
    module_id,
    test_id,
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

#: Why a design-tier item is in ``skipped`` rather than ``failures``
#: (#479). The reason string is part of ``graph-meta.json``, so it names
#: the knob a reader has to change to get the export back.
GRAPH_OPT_OUT = "models.yaml `graph: false`"

#: First ``rtl-buddy-view`` release carrying the ``graph`` subcommand
#: (rtl-buddy-view#126). Mirrors the ``0.3.0`` floor that ``rb
#: hier-query`` gates on in ``tool_manifest.py`` — the manifest floor
#: is what every view-backed command shares, and this is the extra
#: per-feature floor layered on top of it.
VIEW_GRAPH_MIN_VERSION = "0.4.0"

#: Where each tier's own export lands under ``artefacts/graph/``. Kept
#: on disk (not just in memory) so a failed merge is debuggable and so
#: the extractor's ``merge-graphs`` has real files to cross-check against.
DESIGN_SUBDIR = "design"
#: TB-rooted exports nest under their DUT: ``design/<model>/tb/<tb>``.
#: Mirrors ``rb hier --view tb``'s ``artefacts/hier/<model>/tb/<tb>``
#: filelist cache, which is where their sources come from.
TB_SUBDIR = "tb"
#: Run-rooted exports (#385) nest the same way: ``design/<model>/run/<top>``,
#: with the filelist cache at ``artefacts/hier/<model>/run/<top>``.
RUN_SUBDIR = "run"
BINDING_FILE = "binding/graph.json"

#: The in-process binding stage's own export (#378). Kept apart from
#: ``binding/graph.json`` because that file is the extractor's — the binding
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
        # The version quoted here came from `rtl-buddy-view --version`,
        # which still prints the executable's own name — but the thing
        # you install is the renamed `rtl-buddy-sch` dist
        # (rtl-buddy-sch#157; `rtl-buddy-view` is frozen at 0.5.0, below
        # every floor this file states). The uninstall leads because pip
        # has no rename metadata: installing one over the other leaves
        # two dists claiming the same console script. It is a no-op
        # (warning, exit 0) when the old dist was never installed.
        return (
            f"rtl-buddy-view {view_version} has no `graph` subcommand; "
            f"the design tier needs >= {VIEW_GRAPH_MIN_VERSION} "
            f"(pip uninstall -y rtl-buddy-view && "
            f'pip install -U "rtl-buddy-sch >= {VIEW_GRAPH_MIN_VERSION}")'
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
      skipped (list): Per-item opt-outs (#479) — a model marked
        ``graph: false`` in models.yaml, and the testbench / flow-run
        exports that would have re-elaborated it. Deliberately kept apart
        from ``failures``: nothing went wrong, so it must not colour the
        exit code under ``--strict`` or read as noise the project caused.
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
    skipped: list = dc_field(default_factory=list)
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
        if self.skipped:
            block["skipped"] = self.skipped
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
        if self.skipped:
            block["skipped"] = self.skipped
        models = self.extra.get("models")
        if models is not None:
            block["models"] = models
        testbenches = self.extra.get("testbenches")
        if testbenches is not None:
            block["testbenches"] = testbenches
        flow_runs = self.extra.get("flow_runs")
        if flow_runs is not None:
            block["flow_runs"] = flow_runs
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
        flow_runs = self.extra.get("flow_runs")
        if flow_runs is not None:
            parts.append(f"{len(flow_runs)} flow run top(s)")
        collisions = self.extra.get("id_collisions")
        if collisions:
            parts.append(f"{len(collisions)} id(s) suite-qualified")
        if self.failures:
            parts.append(f"{len(self.failures)} failed")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
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
      tb_names (list[str]): Every ``testbenches:`` entry collapsed into
        this export. The de-duplication key is what the viewer is handed
        — model, filelist, top — and deliberately excludes the entry
        *name*, so one suite declaring the same elaboration twice under
        two names elaborates it once. Both names are kept: each is a
        real ``tb:`` node the config tier emitted, owed its own stitch
        and its own row in any per-item report.
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
    tb_names: list[str]
    tb_top: str
    model: ModelConfig
    test: TestConfig

    @property
    def tb_name(self) -> str:
        """The first testbench claiming this export — its short name."""
        return self.tb_names[0]

    @property
    def node_id(self) -> str:
        """Config-tier ``tb:`` node this export belongs to."""
        return testbench_id(self.suite_rel, self.tb_name)

    @property
    def label(self) -> str:
        """``<suite dir>#<tb name>`` — how failures name this target."""
        return f"{self.suite_rel}#{self.tb_name}"

    @property
    def node_ids(self) -> list[str]:
        """One ``tb:`` node per collapsed testbench — each gets a stitch."""
        return [testbench_id(self.suite_rel, name) for name in self.tb_names]

    @property
    def labels(self) -> list[str]:
        """One label per collapsed testbench, as ``node_ids`` is one id."""
        return [f"{self.suite_rel}#{name}" for name in self.tb_names]

    @property
    def stitch_type(self) -> str:
        """Edge type of this target's config->design stitch."""
        return ELABORATES_AS


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
    difference to the *exporter* — but it is one to the report, so every
    collapsed name is remembered in ``tb_names`` and re-expanded by
    ``node_ids`` and ``labels``. Exactly the rule
    :func:`flow_runs_from_regressions` applies to runs.

    A testbench whose top is the DUT top (the model's ``top:`` when it
    declares one, else its name) is dropped: ``--tb-top <that top>``
    would re-elaborate exactly what the DUT export already covered. That
    is the cocotb/SystemC case, where ``toplevel:`` is required *and*
    names the DUT — there is no SV testbench above it to add.

    That drop is conditional on the model being graphable. There is no
    DUT export to defer to when the model opted out (#479), and the
    contract is that *everything* rooted at an opted-out model is listed
    under the design tier's ``skipped``. So a same-root testbench of an
    opted-out model is returned here and refused one step later by
    :func:`_split_opted_out`, which is what turns it into a skip record.
    It is never exported either way — the two paths differ only in
    whether the user is told.

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

    by_key: dict[tuple, TestbenchTarget] = {}
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
            if tb_top == model.get_top() and model.graph:
                continue
            key = (
                suite_dir,
                _model_key(model),
                tuple(tb.get_filelist()),
                tb_top,
            )
            existing = by_key.get(key)
            if existing is not None:
                # Same export, different `testbenches:` entry (or the
                # same one reached through a second test). Remember the
                # name; the export itself is already accounted for.
                if tb.get_name() not in existing.tb_names:
                    existing.tb_names.append(tb.get_name())
                continue
            by_key[key] = TestbenchTarget(
                suite_rel=suite_rel,
                suite_dir=suite_dir,
                tb_names=[tb.get_name()],
                tb_top=tb_top,
                model=model,
                test=test,
            )
    return sorted(by_key.values(), key=lambda t: (t.suite_rel, t.tb_name))


# ---------------------------------------------------------------------------
# Flow-run selection (#385)
#
# A formal/synth/cdc run's `top:` often only elaborates inside the flow's
# own filelist — the template's fpv checker tops live in `properties:`
# files no models.yaml names — so the config tier's `targets` stitch would
# dangle forever if the design tier only exported models and testbenches.
# These runs get the TB treatment: one run-rooted export over the model
# filelist plus the flow's own sources.
# ---------------------------------------------------------------------------


@dataclass
class FlowRunTarget:
    """One run-rooted design-tier export.

    The flow-run counterpart of :class:`TestbenchTarget` — same duck
    type where the qualification machinery is concerned (``suite_rel``,
    ``node_id``, ``tb_top``, ``stitch_type``), different config-tier
    node (``test:``, the run) and different stitch verb (``targets``).

    Attributes:
      suite_rel (str): Repo-relative suite directory — the same string
        the config tier puts in ``test:<suite dir>#<name>``.
      suite_dir (str): Absolute suite directory, anchoring any relative
        entries in ``sources``.
      flow (str): Which flow owns the run (``fpv`` / ``synth`` / ...).
      run_names (list[str]): Every run collapsed into this export — an
        fpv suite proving one checker under ``bmc`` and ``prove``
        elaborates it once, but each run's ``test:`` node still gets its
        own stitch (a suite-qualified top would strand the twins'
        declared edges otherwise).
      top (str): The run's ``top:`` — the module the export is rooted at.
      model (ModelConfig): The DUT whose filelist the flow sources are
        merged on top of.
      sources (list[str]): The flow's own HDL beyond the model filelist
        (an fpv run's ``properties:`` + ``constraints:``; absolute paths,
        the loaders resolve them against the suite dir).
    """

    suite_rel: str
    suite_dir: str
    flow: str
    run_names: list[str]
    top: str
    model: ModelConfig
    sources: list[str]

    @property
    def run_name(self) -> str:
        """The first run claiming this export — its short name."""
        return self.run_names[0]

    @property
    def node_id(self) -> str:
        """Config-tier ``test:`` node this export belongs to."""
        return test_id(self.suite_rel, self.run_name)

    @property
    def node_ids(self) -> list[str]:
        """One ``test:`` node per collapsed run — each gets a stitch."""
        return [test_id(self.suite_rel, name) for name in self.run_names]

    @property
    def label(self) -> str:
        """``<suite dir>#<run name>`` — how failures name this target."""
        return f"{self.suite_rel}#{self.run_name}"

    @property
    def labels(self) -> list[str]:
        """One label per collapsed run, the way ``node_ids`` is one id.

        De-duplication keeps a single export for runs that would produce
        identical bytes, but each of them is a run the user wrote down.
        A per-run record — a skip, here — has to name every one of them,
        or a run silently disappears from the report because a twin
        happened to sort first.
        """
        return [f"{self.suite_rel}#{name}" for name in self.run_names]

    @property
    def tb_top(self) -> str:
        """The top the viewer is asked to elaborate from.

        Named for the ``--tb-top`` mechanism it rides (and for the
        qualification machinery, which handles TB and run targets
        through one code path).
        """
        return self.top

    @property
    def stitch_type(self) -> str:
        """Edge type of this target's config->design stitch."""
        return TARGETS


def _flow_run_sources(flow: str, entry) -> list[str]:
    """The flow-owned HDL a run elaborates beyond the model filelist.

    Only the formal flow has any today: ``properties:`` plus the
    optional ``constraints:`` file, both SystemVerilog by contract and
    both read into the sby script on top of the model sources — so the
    export mirrors exactly what the proof elaborates. Synthesis and CDC
    runs work the model filelist as-is (a ``cdc.yaml`` ``constraints:``
    is an SDC, not HDL).
    """
    if flow != FLOW_FPV:
        return []
    sources = list(entry.get_properties())
    constraints = entry.get_constraints()
    if constraints:
        sources.append(constraints)
    return sources


def flow_runs_from_regressions(
    project_root: str | os.PathLike,
    models: list[ModelConfig] | None = None,
) -> list[FlowRunTarget]:
    """Every non-simulation run worth a run-rooted export, de-duplicated.

    Reads the same repo-level regression files the config tier reads,
    through the same loaders (:func:`_collect_flows`), so the runs
    exported here are exactly the ones that got ``test:`` nodes and
    ``targets`` stitches.

    A run whose ``top:`` is the model's own root module is dropped: the
    DUT export already covers that hierarchy, and today that is every
    synth / cdc / fpga run (their ``get_top()`` is the model's by
    construction). What remains is the formal case — a checker top
    defined in the flow's own filelist.

    As with :func:`testbenches_from_suites`, that drop applies only to a
    graphable model: an opted-out one has no DUT export to defer to, and
    its runs are owed a skip record (#479). They are returned here and
    refused by :func:`_split_opted_out`, never exported.

    Two runs are the same export when they resolve to the same
    ``(suite dir, model, flow sources, top)``: that tuple is the entire
    input to the viewer, exactly as testbench de-duplication reasons.
    An fpv suite proving the same checker under ``bmc`` and ``prove``
    elaborates it once — but every collapsed run is remembered in
    ``run_names``, so each ``test:`` node still gets the observed
    ``targets`` stitch the export produces.

    Args:
      project_root: Root the regression files are discovered under.
      models: When given, only runs against a model in this list are
        returned — the same narrowing ``--model`` / ``--regression``
        applies to the DUT and TB exports. ``None`` means no filtering.

    Returns:
      list[FlowRunTarget]: sorted by ``(suite dir, run name)``.
    """
    root = Path(os.path.realpath(str(project_root)))
    allowed = {_model_key(m) for m in models} if models is not None else None

    by_key: dict[tuple, FlowRunTarget] = {}
    for flow, suite_cfg, entries_attr in _collect_flows(root).suites:
        suite_dir = os.path.dirname(os.path.realpath(suite_cfg.get_path()))
        suite_rel = rel_path(root, suite_dir)
        for entry in getattr(suite_cfg, entries_attr)():
            model = entry.get_model()
            top = entry.get_top()
            if not top:
                continue
            if top == model.get_top() and model.graph:
                continue
            if allowed is not None and _model_key(model) not in allowed:
                continue
            sources = _flow_run_sources(flow, entry)
            key = (suite_dir, _model_key(model), tuple(sources), top)
            existing = by_key.get(key)
            if existing is not None:
                if entry.get_name() not in existing.run_names:
                    existing.run_names.append(entry.get_name())
                continue
            by_key[key] = FlowRunTarget(
                suite_rel=suite_rel,
                suite_dir=suite_dir,
                flow=flow,
                run_names=[entry.get_name()],
                top=top,
                model=model,
                sources=sources,
            )
    return sorted(by_key.values(), key=lambda t: (t.suite_rel, t.run_name))


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


def _flow_exporters(
    project_root: Path,
    targets: list[FlowRunTarget],
    out_dir: Path,
    *,
    view_executable: str,
    frontend: str | None,
) -> list[tuple[FlowRunTarget, RtlBuddyViewGraph]]:
    """One run-rooted exporter per flow run, output paths disambiguated.

    ``design/<model>/run/<top>`` is keyed on the *top* rather than the
    run name because the top is what de-duplication kept unique per
    model — three verifications proving one checker are one export.
    Two suites rooting different files at the same top under the same
    model get a ``-2`` suffix rather than overwriting each other,
    mirroring :func:`_tb_exporters`.
    """
    exporters = []
    used: set[str] = set()
    for target in targets:
        base = f"{target.model.name}/{RUN_SUBDIR}/{target.top}"
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
            run_top=target.top,
            run_filelist=target.sources,
            run_key=slug.partition("/")[2],
            test_suite_dir=target.suite_dir,
        )
        exporters.append((target, exporter))
    return exporters


def _split_opted_out(targets: list, kind: str) -> tuple[list, list[dict]]:
    """Partition TB / flow-run targets by their DUT's ``graph:`` flag (#479).

    A ``graph: false`` model has no elaborable root, and both export
    shapes still hand the viewer ``--top <model top>`` alongside their
    own ``--tb-top`` — so a testbench or flow run over such a model
    would fail for exactly the reason the model opted out. Skipping it
    with a record of its own keeps the reason visible instead of
    silently shrinking the tier.

    One record per *declared* item, not per export: a flow-run target
    collapses runs that would produce identical bytes, and the stitch
    path already re-expands them (``node_ids``). The skip list does the
    same through ``labels``, so an fpv suite proving one checker under
    ``bmc`` and ``prove`` reports both as skipped rather than losing the
    twin that did not happen to sort first.

    Args:
      targets: :class:`TestbenchTarget` / :class:`FlowRunTarget` list.
      kind: ``"testbench"`` or ``"run"`` — the key the skip record uses,
        matching the one its failure rows already use.

    Returns:
      tuple: (targets to export, skip records for the rest).
    """
    keep, skipped = [], []
    for target in targets:
        if target.model.graph:
            keep.append(target)
        else:
            skipped.extend(
                {
                    kind: label,
                    "model": target.model.name,
                    "reason": GRAPH_OPT_OUT,
                }
                for label in target.labels
            )
    return keep, skipped


def _drop_stale_export(out_dir: Path, model: ModelConfig) -> bool:
    """Remove a model's design-tier exports when it opts out (#479).

    The per-model export is a *durable* artefact: ``graph.json`` and the
    viewer's ``graph-meta.json`` sidecar under
    ``artefacts/graph/design/<name>/``, plus the TB- and run-rooted
    exports nested beneath it. Nothing rewrites them but a later export
    of the same model, so a model that was exported yesterday and
    declares ``graph: false`` today would leave that hierarchy on disk,
    fully readable, while the tier report and the merged graph both say
    the model has none. The extractor's cross-check reads those files
    directly, and so does anyone debugging a merge — a stale one is a
    confident wrong answer.

    The whole ``design/<name>/`` subtree is the model's own: the DUT
    export sits at its root and the ``tb/`` and ``run/`` exports nest
    inside it, so removing the subtree removes exactly this model's
    exports and nothing else. Model names are unique across the
    selection (:func:`_reject_colliding_models`), so the directory
    cannot be shared.

    The path is re-checked before the delete, not merely composed. The
    model name is validated where models.yaml is loaded
    (:func:`~rtl_buddy.config.model.validate_model_name`), but this is
    the one place in the graph build that *destroys* data, and a caller
    who hands ``build_graph`` a hand-built :class:`ModelConfig` bypasses
    that loader entirely. So the resolved target must still be a direct
    child of ``design/``: that rules out a name that normalises upwards,
    an absolute one, and a ``design/<name>`` that is a symlink pointing
    somewhere else — none of which ``rmtree`` would think twice about.

    Returns:
      bool: True when something was actually removed.

    Raises:
      FatalRtlBuddyError: when the target is not a direct child of the
        design-export directory, or when the directory is there and cannot be
        removed. Swallowing that would be the worst of both worlds — the
        merged graph and the sidecar would say the model was skipped
        while its old hierarchy stayed on disk and readable, which is
        exactly the state this retraction exists to prevent. An
        unwritable output directory is already the kind of setup problem
        ``build_graph`` propagates rather than degrades. ``rmtree`` is
        also not atomic — it removes what it can before failing — so a
        swallowed error can leave a partial tree that no build produced.
    """
    design_root = out_dir / DESIGN_SUBDIR
    target = design_root / model.name
    if target.resolve().parent != design_root.resolve():
        log_event(
            logger,
            logging.ERROR,
            "graph_build.stale_export_escapes",
            model=model.name,
            path=str(target),
            resolved=str(target.resolve()),
            design_root=str(design_root.resolve()),
        )
        raise FatalRtlBuddyError(
            f"graph build: refusing to retract the export of model "
            f"{model.name!r} — {target} resolves to {target.resolve()}, "
            f"which is not inside {design_root.resolve()}. A model name is "
            f"a directory name; fix it in models.yaml, or remove the symlink "
            f"standing in for that directory."
        )
    if not target.is_dir():
        return False
    try:
        shutil.rmtree(target)
    except OSError as exc:
        log_event(
            logger,
            logging.ERROR,
            "graph_build.stale_export_not_dropped",
            model=model.name,
            path=str(target),
            error=str(exc),
        )
        raise FatalRtlBuddyError(
            f"graph build: model {model.name!r} declares `graph: false`, but "
            f"its previous design-tier export could not be removed: {target} "
            f"({exc}). Leaving it would keep serving a hierarchy this build "
            f"says the model does not have — fix the permissions on that "
            f"directory, or delete it by hand, and re-run."
        ) from exc
    return True


def _model_ident(project_root: Path, model: ModelConfig) -> str:
    """A model's whole design-tier identity, for the build fingerprint.

    ``<models.yaml>#<name> top=<root module> graph=<bool>``. The
    fingerprint's counterpart to :func:`_model_key`, which keys on an
    absolute realpath and so cannot go into a hash that has to reproduce
    across checkouts and machines.

    The *declaration* is part of the identity, not just where it lives.
    A models.yaml under ``--design-dir`` is hashed by the config tier, so
    editing it moves the fingerprint anyway — but one reached only
    through a test's ``model_path:`` (a ``--regression`` selection can
    name a model anywhere) is hashed by nothing. The design tier hashes
    the model's *sources*, and neither ``top:`` nor ``graph:`` changes
    those. Without them here, re-rooting such a model left the
    fingerprint untouched and ``graph build`` served a cached graph
    rooted at the module the model used to name (#479).
    """
    rel = rel_path(project_root, model.path) if model.path else "?"
    return f"{rel}#{model.name} top={model.get_top()} graph={bool(model.graph)}"


def _claimants(project_root: Path, models: list[ModelConfig]) -> str:
    """``name (models.yaml), name (models.yaml)`` — who is in a collision.

    The path is what makes the message actionable: the two entries are
    in different files by construction (a collision *within* one
    ``models.yaml`` never reaches here — the loader is already fatal on
    a duplicate ``name:``), so the name alone would not say where to go.
    """
    return ", ".join(
        f"{m.name} ({rel_path(project_root, m.path) if m.path else '?'})"
        for m in models
    )


def _grouped(models: list[ModelConfig], key) -> dict[str, list[ModelConfig]]:
    """Models bucketed by ``key``, keeping only the buckets with a clash."""
    buckets: dict[str, list[ModelConfig]] = {}
    for model in models:
        buckets.setdefault(key(model), []).append(model)
    return {k: ms for k, ms in sorted(buckets.items()) if len(ms) > 1}


def _reject_colliding_models(
    project_root: Path, models: list[ModelConfig], graphable: list[ModelConfig]
) -> None:
    """Refuse a design tier two models would land in the same slot (#479).

    Two collisions, both of which produce a graph that reads as correct
    and is not, and neither of which anything downstream can detect —
    which is why both are refused here rather than reported afterwards.

    **Same ``name:``, checked across every model in scope, opted out or
    not.** Every per-model artefact path is keyed on the model name: the
    export lands in ``artefacts/graph/design/<name>/`` and its generated
    filelist in ``artefacts/hier/<name>/``. Two models of one name in two
    ``models.yaml`` files are distinct entries everywhere else
    (``_model_key`` is realpath-qualified, and so are their ``model:``
    node ids), so both are planned, both run, and the second silently
    overwrites the first — while the tier reports both as built and the
    merge takes whichever bytes survived. A duplicate *within* one file
    is already fatal in
    :class:`~rtl_buddy.config.model.ModelConfigLoader`; this is the
    across-files half of that rule.

    ``graph: false`` is not a way out of *this* half. A model name is how
    every selector spells a model — ``rb graph build --model NAME``, a
    test's ``model:``, a back-pointer — and none of them can say which of
    two entries is meant. An opted-out duplicate is therefore still a
    name two files are fighting over: it would shadow the graphable one
    in a name-keyed lookup, silently, and the shadowing is invisible
    afterwards because the surviving entry looks like the only one.

    **Same top.** A design-tier export's ids are **global** by contract —
    ``module:<top>``, ``inst:<top>/…`` — and DUT ids are deliberately the
    one thing suite qualification never touches: they are the weld a TB
    or run export merges onto (see the id-collision section below). So
    two models exporting the same top do not produce two hierarchies.
    :func:`~rtl_buddy.graph.merge.merge_graphs` keeps the first node's
    attributes and unions both link sets, and what lands in
    ``graph.json`` is one module node wearing one model's file and line
    while instantiating both designs' children. ``top:`` is what makes
    this reachable on purpose, but two same-named models have always
    collided this way too.

    Names are checked first: it is the more basic identity problem, and
    when both hold, "rename one model" is the instruction that fixes
    both.

    The top half is graphable-only: a ``graph: false`` model is never
    handed to the viewer, so it claims no graph id. Both halves see only
    the *selected* models, so ``--model`` / ``-c`` narrow the check
    exactly as they narrow the tier.

    Args:
      models: every model in scope, including the opted-out ones.
      graphable: the subset that will actually be exported.

    Raises:
      FatalRtlBuddyError: naming every model in the collision, the
        ``models.yaml`` each comes from, and the ways out.
    """
    for name, claimants in _grouped(models, lambda m: m.name).items():
        log_event(
            logger,
            logging.ERROR,
            "graph_build.duplicate_design_model",
            model=name,
            paths=", ".join(
                rel_path(project_root, m.path) for m in claimants if m.path
            ),
        )
        raise FatalRtlBuddyError(
            f"graph build: {len(claimants)} models are named {name!r}, and "
            f"every per-model artefact path — and every selector that "
            f"names a model — is keyed on that name; their exports would "
            f"overwrite each other in artefacts/graph/design/{name}/ and "
            f"artefacts/hier/{name}/:\n"
            f"  name {name!r} is claimed by: "
            f"{_claimants(project_root, claimants)}\n"
            f"Rename one of them. `graph: false` does not resolve a name "
            f"collision — the opted-out entry would still shadow the other "
            f"in any lookup by name."
        )

    clashes = _grouped(graphable, lambda m: m.get_top())
    if not clashes:
        return
    lines = []
    for top, claimants in clashes.items():
        lines.append(
            f"  top {top!r} is claimed by: {_claimants(project_root, claimants)}"
        )
        log_event(
            logger,
            logging.ERROR,
            "graph_build.duplicate_design_top",
            top=top,
            models=", ".join(m.name for m in claimants),
            paths=", ".join(
                rel_path(project_root, m.path) for m in claimants if m.path
            ),
        )
    raise FatalRtlBuddyError(
        "graph build: two or more models would be exported with the same "
        "top module, which the design tier cannot keep apart — "
        "`module:<top>` is a global id, so the exports would merge into "
        "one hybrid hierarchy:\n"
        + "\n".join(lines)
        + "\nGive them distinct roots with `top:` in models.yaml, or set "
        "`graph: false` on the one that is not the design of record."
    )


def _stitch_link(node_id: str, module_node_id: str, link_type: str) -> dict:
    """``<config node> --elaborates_as|targets--> module:<top>``.

    The observed twin of the config tier's declared stitch: a ``tb:``
    node (or a flow run's ``test:`` node) is metadata about an
    elaboration, and this edge says which design module that elaboration
    is topped by. It is the same relation the model node's ``maps_to``
    states, spelled with the source's own verb so a reader never has to
    recover the source kind from the id prefix.
    """
    return {
        "source": node_id,
        "target": module_node_id,
        "type": link_type,
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


def _run_flow_tier(
    project_root: Path,
    exporters: list[tuple[FlowRunTarget, RtlBuddyViewGraph]],
    report: TierReport,
) -> list[tuple[FlowRunTarget, dict]]:
    """Invoke the viewer per flow run; return the graphs that came back.

    Same contract as :func:`_run_tb_tier`: one broken run costs its own
    hierarchy, never the tier, and its failure row names the run.

    ``report.extra["flow_runs"]`` counts **exports**, not runs — a suite
    proving one checker under ``bmc`` and ``prove`` elaborates it once —
    so a collapsed export names its extra runs in parentheses rather than
    letting a reader of ``graph-meta.json`` infer that the twin was
    dropped. Every collapsed run still gets its own ``targets`` stitch.
    """
    pairs: list[tuple[FlowRunTarget, dict]] = []
    built: list[str] = []
    for target, exporter in exporters:
        try:
            rc = exporter.run()
        except FatalRtlBuddyError as exc:
            report.failures.append({"run": target.label, "error": str(exc)})
            continue
        if rc != 0:
            report.failures.append(
                {
                    "run": target.label,
                    "error": f"rtl-buddy-view graph exited {rc}",
                    "log": rel_path(project_root, exporter.log_path()),
                }
            )
            log_event(
                logger,
                logging.WARNING,
                "graph_build.run_export_failed",
                run=target.label,
                top=target.top,
                returncode=rc,
                log=exporter.log_path(),
            )
            continue
        try:
            payload = json.loads(Path(exporter.output).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            report.failures.append({"run": target.label, "error": str(exc)})
            continue
        pairs.append((target, payload))
        # One entry per *export*, not per run — but a de-duplicated export
        # names every run it collapsed, so `graph-meta.json` never reads as
        # if the twin had been dropped (it gets its own `targets` stitch).
        built.append(
            target.label
            if len(target.run_names) == 1
            else f"{target.label} (+{', '.join(target.run_names[1:])})"
        )
        if report.generator is None:
            report.generator = (payload.get("graph") or {}).get("generator")
    report.extra["flow_runs"] = built
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


def _collision_base_name(original: str) -> str | None:
    """The module name behind a collision entry that gets an indexed label.

    ``module:X`` and ``inst:X/X`` (the root scope of X's own elaboration)
    both resolve to ``X``; anything else — ports, child instances — is
    not label-indexed and returns None.
    """
    if original.startswith("module:"):
        return original[len("module:") :]
    if original.startswith("inst:"):
        root, _, rest = original[len("inst:") :].partition("/")
        if rest == root:
            return root
    return None


def _index_collision_labels(graphs: list[dict], collisions: dict[str, dict]) -> None:
    """Give each colliding testbench top a rendered label of ``name(i)``.

    Reusing a conventional top name (``tb_top``) across suites is a
    supported pattern — the *ids* stay apart via the suite qualifier, but
    N nodes all labelled ``tb_top`` are indistinguishable on the graph
    pane. So the module node (and its root-scope instance) get a
    deterministic short label ``tb_top(0)`` … ``tb_top(N-1)``.

    The index is keyed on (base name, qualifier) and derived from the
    **union** of the ``module:X`` and ``inst:X/X`` entries' qualifiers —
    not per entry — because the two entries can carry different suite
    sets (a suite where ``X`` is a module but not the elaboration top
    joins the module entry only), and indexing them independently could
    render one suite as ``tb_top(2)`` on the module and ``tb_top(1)`` on
    its root instance. Keying on the qualifier makes the guarantee
    structural. Sorting by qualifier = sorting by suite path, so the
    index is stable across rebuilds.

    The original name stays in ``base_label`` (and inside
    ``unqualified_id``); query scoring and node resolution treat
    ``base_label`` like ``label``, so ``tb_top`` still matches at the
    exact-name tier. Deeper nodes (ports, child instances) keep their
    own labels — they render nested under an indexed parent.
    """
    base_qualifiers: dict[str, set[str]] = {}
    for original, entry in collisions.items():
        base = _collision_base_name(original)
        if base is None:
            continue
        for qualified_id in entry["qualified"]:
            _, sep, qualifier = qualified_id.rpartition(QUALIFIER_SEP)
            if sep and qualifier:
                base_qualifiers.setdefault(base, set()).add(qualifier)
    index = {
        base: {q: i for i, q in enumerate(sorted(qualifiers))}
        for base, qualifiers in base_qualifiers.items()
    }
    labelled: dict[str, str] = {}
    for graph in graphs:
        for node in graph.get("nodes") or []:
            node_id = node.get("id")
            label = node.get("label")
            qualifier = node.get("qualified_by")
            if not node_id or not label or not qualifier:
                continue
            unqualified = node.get("unqualified_id") or ""
            is_module_self = (
                node.get("type") == "module" and unqualified == f"module:{label}"
            )
            is_root_instance = (
                node.get("type") == "instance"
                and unqualified == f"inst:{label}/{label}"
            )
            if not (is_module_self or is_root_instance):
                continue
            i = index.get(label, {}).get(qualifier)
            if i is None:
                continue
            node["base_label"] = label
            node["label"] = f"{label}({i})"
            labelled[node_id] = node["label"]
    for entry in collisions.values():
        labels = [labelled[q] for q in entry["qualified"] if q in labelled]
        if labels:
            entry["labels"] = labels


def _qualify_tb_graphs(
    model_graphs: list[dict],
    pairs: list[tuple[TestbenchTarget | FlowRunTarget, dict]],
    report: TierReport,
) -> tuple[list[dict], list[dict]]:
    """Resolve TB/run id collisions and emit the config -> ``module:`` stitches.

    The stitch points at the top the viewer *actually* elaborated
    (``graph.design.top``, which it auto-corrects when the ``--tb-top``
    hint names no real module), after qualification — so the edge always
    lands on a node that exists and is the right one. Its type is the
    target's own verb: ``elaborates_as`` for a testbench, ``targets``
    for a flow run — the qualification machinery is shared, the stitch
    vocabulary is not.
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
        # One stitch per config node the export answers for: a flow-run
        # export de-duplicated across several runs stitches each of their
        # `test:` nodes, or a qualified top would strand the twins.
        for node_id in target.node_ids:
            stitches.append(
                _stitch_link(node_id, rename.get(root_id, root_id), target.stitch_type)
            )
        for original, new_id in rename.items():
            entry = collisions.setdefault(
                original,
                {"id": original, "files": ambiguous.get(original, []), "qualified": []},
            )
            entry["qualified"].append(new_id)
    if collisions:
        for entry in collisions.values():
            # Sorted + deduped is a *stated* property of the meta payload
            # (docs/concepts/graph.md): it is what the label index derives
            # from, so it is established here where the entry is built,
            # not as a labelling side effect.
            entry["qualified"] = sorted(set(entry["qualified"]))
        _index_collision_labels(graphs, collisions)
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
    flow_tops: bool = True,
    bind: bool = True,
    extract_enabled: bool = True,
    extract_executable: str = extract_mod.GRAPH_EXTRACT_BINARY,
    extract_cross_check: bool = True,
    extract_version: str | None = None,
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
      flow_tops: False skips the run-rooted exports (#385) — the
        formal/synth/cdc run tops that only elaborate inside their
        flow's own filelist. The same kind of cost switch as ``tb``.
      bind: False skips the post-merge binding stage (#378) — no
        ``binds_to`` / ``drives`` / ``checks_against`` edges.
      extract_enabled: False skips the extractor's binding tier without
        probing for the tool.
      extract_cross_check: Run the extractor's ``merge-graphs`` and
        compare against the internal union.
      force: Rebuild even when the fingerprint is unchanged.

    Returns:
      GraphBuild: paths, per-tier reports, and whether anything ran.

    Never raises for a tier that could not be built — inspect
    ``failed_tiers()`` / ``has_failures()``. Only a genuinely
    unrecoverable setup problem (unreadable regression config, unwritable
    output directory, two graphable models rooted at the same module)
    propagates.
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
    flow_exporters: list[tuple[FlowRunTarget, RtlBuddyViewGraph]] = []
    # What the design tier selected, kept outside the branches below so
    # the fingerprint can see it whichever way the tier resolved.
    graphable: list[ModelConfig] = []
    tb_targets: list[TestbenchTarget] = []
    run_targets: list[FlowRunTarget] = []
    design_report = TierReport(tier=DESIGN_TIER)
    if not design:
        design_report.status = SKIPPED
        design_report.detail = "disabled (--no-design)"
    elif not models:
        design_report.status = SKIPPED
        design_report.detail = f"no models found under {rel_path(root, search_design)}"
    else:
        # #479: a model that declares `graph: false` has no elaborable
        # root — an SV `interface` published as a library entry, a
        # filelist of vendored IP with no module named after the model.
        # It is recorded as skipped and never handed to the viewer, so
        # the project stops carrying a permanent failure row it cannot
        # silence. The config tier still emits its `model:` node.
        #
        # The whole partition happens *before* the viewer version gate:
        # what is left after it is the answer to "does this tier need the
        # viewer at all", and an outdated viewer must not fail a tier that
        # was never going to invoke it. Target discovery is config reading
        # only — the config tier reads the same files a few lines below.
        graphable = [model for model in models if model.graph]
        # First, before anything is planned, exported *or deleted*: no two
        # models in scope may share a name (their artefact paths and every
        # name-keyed lookup collide, opt-out or not), and no two graphable
        # ones may share a top (their graph ids collide).
        #
        # The ordering is load-bearing, not tidiness. `design/<name>/` is
        # keyed on the name, so a colliding pair *shares* that directory —
        # and the retraction below would delete it on the opted-out
        # model's behalf before the collision was reported. The command
        # would fail as intended and destroy the graphable model's export
        # on the way out, for a configuration it never accepted.
        _reject_colliding_models(root, models, graphable)
        for model in models:
            if model.graph:
                continue
            design_report.skipped.append({"model": model.name, "reason": GRAPH_OPT_OUT})
            # The export is durable, so opting out has to retract it —
            # otherwise `design/<name>/graph.json` keeps serving the
            # hierarchy this build just declared the model does not have.
            if _drop_stale_export(out, model):
                log_event(
                    logger,
                    logging.INFO,
                    "graph_build.stale_export_dropped",
                    model=model.name,
                    path=str(out / DESIGN_SUBDIR / model.name),
                )
        if tb:
            tb_targets, opted_out = _split_opted_out(
                testbenches_from_suites(root, search_verif, models), "testbench"
            )
            design_report.skipped.extend(opted_out)
        if flow_tops:
            run_targets, opted_out = _split_opted_out(
                flow_runs_from_regressions(root, models), "run"
            )
            design_report.skipped.extend(opted_out)

        gate = check_view_supports_graph(view_version)
        if not (graphable or tb_targets or run_targets):
            # Everything in scope opted out. Nothing broke, so the tier is
            # skipped rather than failed — a project whose design dir holds
            # only library models must still exit 0, whatever viewer it has.
            design_report.status = SKIPPED
            design_report.detail = (
                f"every model in scope opted out ({len(design_report.skipped)} skipped)"
            )
        elif gate is not None:
            design_report.status = FAILED
            design_report.detail = gate
        else:
            tools["rtl-buddy-view"] = view_version
            sources: list[str] = []
            for model, exporter in _design_exporters(
                root,
                graphable,
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
            if tb_targets:
                for target, exporter in _tb_exporters(
                    root,
                    tb_targets,
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
            # Run-rooted exports (#385): same tier, same filelist
            # machinery, `--tb-top` carrying the run's `top:` over the
            # model filelist + the flow's own sources. Those sources
            # join the tier's input hashes too, so editing a properties
            # file invalidates the cached graph.
            if run_targets:
                for target, exporter in _flow_exporters(
                    root,
                    run_targets,
                    out,
                    view_executable=view_executable,
                    frontend=frontend,
                ):
                    try:
                        exporter.write_filelist()
                    except Exception as exc:  # FilelistError and friends
                        design_report.failures.append(
                            {"run": target.label, "error": str(exc)}
                        )
                        continue
                    sources.extend(exporter.source_files())
                    flow_exporters.append((target, exporter))
            design_report.inputs = hash_inputs(root, sources)
            if not exporters and not tb_exporters and not flow_exporters:
                # Reachable only when something in scope *was* graphable
                # and none of it produced a filelist — the all-opted-out
                # case short-circuited to SKIPPED before the gate above.
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

    # --- binding tier (extractor, optional) -----------------------------
    binding_report = TierReport(tier=BINDING_TIER)
    extract_inputs: list[str] = []
    if not extract_enabled:
        binding_report.status = SKIPPED
        binding_report.detail = "disabled (--no-extract)"
    elif extract_version is None:
        binding_report.status = SKIPPED
        binding_report.detail = (
            "no binding-tier extractor installed — design + config tiers "
            "only (run `rb tool-check --explain rtl-buddy-graph-extract`)"
        )
    else:
        tools[extract_mod.GRAPH_EXTRACT_TOOL] = extract_version
        extract_inputs = extract_mod.collect_inputs(search_verif, search_spec)
        if not extract_inputs:
            binding_report.status = SKIPPED
            binding_report.detail = "no verif Python or spec markdown found"
    # The in-process binding stage reads verif/spec Python (and C/C++, for
    # the DPI symbol scan) whether or not an extractor is installed, so its
    # inputs belong in the tier's hash list unconditionally: editing a cocotb
    # test or a DPI reference model must invalidate the cache.
    bind_inputs = collect_sources(search_verif, search_spec) if bind else []
    binding_report.inputs = hash_inputs(root, sorted(set(extract_inputs + bind_inputs)))
    reports[BINDING_TIER] = binding_report

    # --- no-op check ----------------------------------------------------
    tier_inputs = {name: report.inputs for name, report in reports.items()}
    # What this invocation *chose* to cover, alongside what it read.
    # Inputs alone are not enough to decide a re-run is a no-op: a design
    # tier whose models all declared `graph: false` hashes nothing, so
    # narrowing it with `--model` — or dropping `--tb` / `--flow-tops` —
    # moves nothing in `tier_inputs`, the fingerprint matches, and the
    # build hands back a `graph-meta.json` whose `skipped` list describes
    # the *previous* invocation (#479). The selectors and the opt-out
    # records are part of what the sidecar reports, so they are part of
    # what makes it stale. Identities are repo-relative, so the
    # fingerprint still reproduces across checkouts.
    #
    # `models` covers every *selected* model, opted out or not, and each
    # entry carries its `top:` and `graph:`. Membership alone would in
    # fact catch an opt-out — the model leaves the exported set and gains
    # a skip record, and both are here — but that leans on two derived
    # lists agreeing, where the declaration itself is the thing that
    # changed. `top:` has no such indirect route at all.
    selection = {
        DESIGN_TIER: {
            "enabled": design,
            "tb": tb,
            "flow_tops": flow_tops,
            "models": sorted(_model_ident(root, m) for m in models),
            "testbenches": sorted(t.label for t in tb_targets),
            "flow_runs": sorted(t.label for t in run_targets),
            "skipped": sorted(
                json.dumps(record, sort_keys=True) for record in design_report.skipped
            ),
        },
        BINDING_TIER: {"bind": bind, "extract": extract_enabled},
    }
    fp = fingerprint(
        schema_version=SCHEMA_VERSION,
        tools=tools,
        tier_inputs=tier_inputs,
        selection=selection,
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
    if exporters or tb_exporters or flow_exporters:
        design_graphs = _run_design_tier(root, exporters, design_report)
        pairs: list[tuple[TestbenchTarget | FlowRunTarget, dict]] = []
        if tb_exporters:
            pairs += _run_tb_tier(root, tb_exporters, design_report)
        if flow_exporters:
            pairs += _run_flow_tier(root, flow_exporters, design_report)
        if pairs:
            tb_graphs, tb_stitches = _qualify_tb_graphs(
                design_graphs, pairs, design_report
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
                for _, e in [*exporters, *tb_exporters, *flow_exporters]
                if Path(e.output).is_file()
            )
        else:
            design_report.status = FAILED
            design_report.detail = "no model exported successfully"

    # The `tb:` node (and a flow run's `test:` node) belongs to the
    # config tier, so its stitch to the hierarchy it elaborates is a
    # config-tier link — the same asymmetry `model --maps_to--> module:`
    # already has. It can only be written after the export, because only
    # the export knows the top the viewer really elaborated (a testbench
    # may declare no `toplevel:` at all) and whether that id had to be
    # suite-qualified. Where both exist the export wins: the config
    # tier's `toplevel:`- / `top:`-derived edge is a declaration, this
    # one is an observation of the same thing. Matching is on (source,
    # type) so a run's declared `targets` is replaced without touching
    # its other edges.
    if tb_stitches:
        exported = {(link["source"], link["type"]) for link in tb_stitches}
        config.graph["links"] = [
            link
            for link in config.graph["links"]
            if (link["source"], link["type"]) not in exported
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
        result = extract_mod.run_extract(
            extract_inputs,
            binding_file,
            executable=extract_executable,
            log_path=out / "extract.log",
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
                "graph_build.extract_failed",
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
            verif_dir=search_verif,
            spec_dir=search_spec,
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
                # The DPI pass's counters ride the same line: this is the
                # one event that tells a machine-mode consumer it ran.
                dpi=stage.dpi_functions,
                implemented=stage.dpi_implemented,
            )

    merge_info: dict = {
        "strategy": "node-id-union",
        # De-duplicated: the binding tier has two producers (the extractor and
        # the post-merge stage) and contributes two graphs, but it is
        # still one tier.
        "tiers": list(dict.fromkeys(tier for tier, _ in tier_graphs)),
        "stitch_points": len(stitch_points(tier_graphs)),
        "dangling": dangling_targets(merged),
    }
    if extract_version is not None and extract_cross_check:
        merge_info["extract_cross_check"] = extract_mod.run_merge_cross_check(
            tier_files,
            out / "extract-merged.json",
            internal=merged,
            executable=extract_executable,
            log_path=out / "extract.log",
            cwd=str(root),
        )
    else:
        merge_info["extract_cross_check"] = {
            "status": "skipped",
            "detail": "extractor not installed"
            if extract_version is None
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
        if not report.skipped:
            report.skipped = stored.get("skipped") or []
        for key in ("models", "testbenches", "flow_runs"):
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
