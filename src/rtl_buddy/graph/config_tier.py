# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Config-tier extractor for the design knowledge graph (#376).

``tests.yaml`` / ``models.yaml`` / ``specs.yaml`` already spell out the
design <-> verif <-> spec relationships (``testbench:``, ``model:``,
``covers:``, ``spec:``, ``docs:``, ``coverage-items``). This module turns
them into graph nodes and edges *deterministically* — every link is
tagged ``EXTRACTED``, nothing is inferred by an LLM.

The repo-level regression files (``regression.yaml``,
``synth_regression.yaml``, ``fpv_regression.yaml``, ``cdc_regression.yaml``,
``fpga_regression.yaml``) add the one thing a suite config cannot say about
itself: which *flow* runs it. Every suite, test and testbench node carries a
``flow`` stamp, and the non-simulation flows' suites — which no ``verif/``
walk would ever reach — become nodes of the same three types.

Everything here reads through the existing loaders
(:mod:`rtl_buddy.config.spec`, :mod:`~rtl_buddy.config.model`,
:mod:`~rtl_buddy.config.suite`, and each flow's ``*RegConfig``) and the
discovery helpers in
:mod:`rtl_buddy.tools.spec_trace` that ``rb spec check-coverage`` and
``rb spec check-design`` use, so the graph cannot disagree with those
commands. There is no second YAML parser.

The emitted envelope is NetworkX node-link JSON so the tiers can be
merged by node-id union; see ``docs/concepts/graph.md`` for the shared
contract. Volatile data (pass/fail, seeds, artefact paths) is
deliberately absent — that is the results overlay's job (#379).

No CLI is wired up here; ``rb graph ...`` arrives in #377. The entry
points are :func:`build_config_tier` and :func:`extract_config_tier`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field as dc_field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from serde.yaml import from_yaml

from ..config.cdc import CdcRegConfig
from ..config.fpga import FpgaRegConfig
from ..config.lint import LintRegConfig
from ..config.fpv import FpvRegConfig
from ..config.reg import RegConfig
from ..config.root import load_reg_cfg_paths, resolve_reg_cfg_path
from ..config.spec import SpecBlock, SpecConfig
from ..config.suite import SuiteConfig, SuiteConfigFile
from ..config.synth import SynthRegConfig
from ..config.test import TestbenchConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..tools.spec_trace import (
    _walk_yaml_files,
    build_spec_to_models_map,
    discover_model_configs,
    discover_spec_configs,
)

logger = logging.getLogger(__name__)

#: Bumped whenever the node/edge vocabulary changes incompatibly.
SCHEMA_VERSION = 1

#: ``generator.tier`` value stamped on every graph this module produces.
CONFIG_TIER = "config"

# Defined in `tools.artifact_paths` — the bottom of the import graph, and
# where the artefact-clearing helpers protect it from a co-named run's
# suffix clear (#469). Re-exported here, where consumers already look.
from ..tools.artifact_paths import (  # noqa: E402
    GRAPH_JSON_NAME as GRAPH_JSON_NAME,
    GRAPH_META_NAME as GRAPH_META_NAME,
)

#: Confidence tag for links. The config tier is pure config readback, so
#: every link it emits is EXTRACTED — INFERRED/AMBIGUOUS are reserved for
#: the binding tier's ``dut.<signal>`` scan (#378).
EXTRACTED = "EXTRACTED"

#: The three config->design stitches. Same relation ("this config thing
#: is that design module"), same direction, same provenance rules — but
#: three edge *types*, one per source kind, because the source kind is
#: the thing a consumer keeps asking about and a single verb threw it
#: away. Recovering it meant re-deriving it from the source id prefix at
#: every read site, which is a fact the edge already knew.
MAPS_TO = "maps_to"  #: model -> the module it names
ELABORATES_AS = "elaborates_as"  #: testbench -> the top it elaborates
TARGETS = "targets"  #: non-simulation run -> its ``top:``

#: Suffixes scanned when looking for verif-side references to a golden
#: model. Text formats only; anything else is skipped unread.
_VERIF_SOURCE_SUFFIXES = (".py", ".sv", ".svh", ".v", ".vh", ".yaml", ".yml", ".f")

#: Directories never descended into while scanning verif sources.
_SKIP_DIRS = frozenset(
    {".git", "__pycache__", "artefacts", "obj_dir", "node_modules", "venv", ".venv"}
)

#: Largest verif source file read during the golden-model reference scan.
_MAX_SCAN_BYTES = 1 << 20

# ---------------------------------------------------------------------------
# Flow provenance
#
# A project runs the same design through several flows, and each has its own
# repo-level regression file listing the suites it owns. Those files are the
# only place that says "this suite is a formal suite, that one is a CDC
# suite" — a `tests.yaml` on its own does not know. Reading them here turns
# `flow` into a node attribute, which is what lets a consumer (the hub pane,
# #382) group by flow instead of by tier.
# ---------------------------------------------------------------------------

#: Simulation. `tests.yaml` suites under `verif/`, listed by `regression.yaml`.
FLOW_SIM = "sim"
#: Synthesis. `synth.yaml` suites, listed by `synth_regression.yaml`.
FLOW_SYNTH = "synth"
#: Formal property verification. `fpv.yaml` / `fpv_regression.yaml`.
FLOW_FPV = "fpv"
#: CDC lint. `cdc.yaml` / `cdc_regression.yaml`.
FLOW_CDC = "cdc"
#: FPGA implementation. `fpga.yaml` / `fpga_regression.yaml`.
FLOW_FPGA = "fpga"
#: Style lint (verible). `lint.yaml` / `lint_regression.yaml`.
FLOW_LINT = "lint"

#: Flow assumed for a suite no repo-level regression file claims. A
#: `tests.yaml` that simply is not wired into `regression.yaml` yet is still
#: a simulation suite, and dropping it into an "unknown" bucket would hide
#: the suites a project is in the middle of adding.
DEFAULT_FLOW = FLOW_SIM


@dataclass(frozen=True)
class _FlowSource:
    """One repo-level regression file and how to walk what it lists.

    ``entries`` is the accessor on each listed suite config that returns
    that flow's runs (``get_syntheses``, ``get_verifications``, ...). The
    simulation flow leaves it empty: its suites are already emitted by the
    ``verif/`` walk, so ``regression.yaml`` only contributes the stamp.
    """

    flow: str
    filename: str
    loader: type
    entries: str = ""


#: The flow sources, in the order they are read. Discovery is by filename at
#: the project root, then by the flow's `cfg-rtl-reg` path from
#: `root_config.yaml` — the same precedence `rb <flow>-regression` applies
#: when no `-c` is passed, so a manifest kept away from the root (e.g.
#: `cdc_regression.yaml` under `lint/cdc/`) is only visible to the graph
#: if the command would find it too (#389).
FLOW_SOURCES: tuple[_FlowSource, ...] = (
    _FlowSource(FLOW_SIM, "regression.yaml", RegConfig),
    _FlowSource(FLOW_SYNTH, "synth_regression.yaml", SynthRegConfig, "get_syntheses"),
    _FlowSource(FLOW_FPV, "fpv_regression.yaml", FpvRegConfig, "get_verifications"),
    _FlowSource(FLOW_CDC, "cdc_regression.yaml", CdcRegConfig, "get_analyses"),
    _FlowSource(FLOW_FPGA, "fpga_regression.yaml", FpgaRegConfig, "get_runs"),
    _FlowSource(FLOW_LINT, "lint_regression.yaml", LintRegConfig, "get_checks"),
)


def _tool_version() -> str:
    try:
        return version("rtl-buddy")
    except PackageNotFoundError:  # pragma: no cover - only in odd installs
        return "0+unknown"


# ---------------------------------------------------------------------------
# Node ids
#
# Ids are the merge keys across tiers, so they are built here and nowhere
# else. Every path component is repo-relative and posix-separated so an
# id computed on macOS matches one computed on Linux.
# ---------------------------------------------------------------------------


def suite_id(suite_dir_rel: str) -> str:
    return f"suite:{suite_dir_rel}"


def test_id(suite_dir_rel: str, name: str) -> str:
    return f"test:{suite_dir_rel}#{name}"


def testbench_id(suite_dir_rel: str, name: str) -> str:
    return f"tb:{suite_dir_rel}#{name}"


def model_id(models_yaml_rel: str, name: str) -> str:
    return f"model:{models_yaml_rel}#{name}"


def spec_block_id(block_name: str) -> str:
    return f"spec:{block_name}"


def coverage_item_id(block_name: str, item_id: str) -> str:
    return f"covitem:{block_name}#{item_id}"


def spec_doc_id(doc_rel: str) -> str:
    return f"doc:{doc_rel}"


def golden_model_id(path_rel: str) -> str:
    return f"golden:{path_rel}"


def module_id(module_name: str) -> str:
    """Design-tier module id.

    The config tier never *creates* these nodes — ``rtl-buddy-view``
    owns them. It only points its three config->design stitches
    (``maps_to`` / ``elaborates_as`` / ``targets``) at them, and the
    shared id is what stitches the two tiers together at merge time.
    """
    return f"module:{module_name}"


# ---------------------------------------------------------------------------
# Accumulator
# ---------------------------------------------------------------------------


@dataclass
class _GraphBuilder:
    """Collects nodes and links, de-duplicating by id / by whole link."""

    nodes: dict[str, dict] = dc_field(default_factory=dict)
    links: dict[tuple[str, str, str], dict] = dc_field(default_factory=dict)

    def add_node(self, node_id: str, node_type: str, label: str, **attrs) -> str:
        clean = {k: v for k, v in attrs.items() if v is not None}
        existing = self.nodes.get(node_id)
        if existing is not None:
            if existing["type"] != node_type:
                log_event(
                    logger,
                    logging.WARNING,
                    "graph_config.node_id_conflict",
                    node=node_id,
                    first_type=existing["type"],
                    second_type=node_type,
                )
                return node_id
            # Same id, same type: fill in attributes the first sighting
            # lacked rather than overwrite what it established.
            for key, value in clean.items():
                existing.setdefault(key, value)
            return node_id
        self.nodes[node_id] = {
            "id": node_id,
            "type": node_type,
            "label": label,
            "tier": CONFIG_TIER,
            **clean,
        }
        return node_id

    def add_link(self, source: str, target: str, link_type: str, **attrs) -> None:
        key = (source, target, link_type)
        if key in self.links:
            return
        self.links[key] = {
            "source": source,
            "target": target,
            "type": link_type,
            "confidence": EXTRACTED,
            **{k: v for k, v in attrs.items() if v is not None},
        }

    def node_list(self) -> list[dict]:
        return [self.nodes[k] for k in sorted(self.nodes)]

    def link_list(self) -> list[dict]:
        return [self.links[k] for k in sorted(self.links)]


@dataclass
class ConfigTier:
    """Result of one config-tier extraction.

    Attributes:
      graph (dict): NetworkX node-link JSON, ready to write as ``graph.json``.
      meta (dict): provenance sidecar for ``graph-meta.json`` — generator
        identity plus the content hash of every config file read. Kept out
        of ``graph`` on purpose: hashes churn on every edit, and merging
        tiers must not have to reconcile them.
      suite_load_failures (list[str]): repo-relative ``tests.yaml`` paths
        that failed to load. Extraction is best-effort, the same as
        ``rb spec check-coverage``; callers decide whether to fail.
    """

    graph: dict
    meta: dict
    suite_load_failures: list[str] = dc_field(default_factory=list)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _rel(project_root: Path, path: str | os.PathLike) -> str:
    """Repo-relative, posix-separated path used inside node ids.

    Both sides go through ``realpath`` first: a ``models.yaml`` reached
    via ``../../design/x/models.yaml`` from a suite dir and the same file
    found by the design-tree walk must produce one identical id, or the
    ``exercises`` edge would dangle. Paths outside the project root are
    returned absolute (they cannot be made repo-relative meaningfully).
    """
    resolved = Path(os.path.realpath(str(path)))
    root = Path(os.path.realpath(str(project_root)))
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def default_graph_dir(project_root: str | os.PathLike) -> Path:
    """``<project root>/artefacts/graph`` — the contracted output dir."""
    return Path(project_root) / "artefacts" / "graph"


# ---------------------------------------------------------------------------
# Flow discovery
# ---------------------------------------------------------------------------


@dataclass
class _Flows:
    """What the repo-level regression files said.

    Attributes:
      by_suite (dict[str, list[str]]): suite dir (repo-relative) -> the
        flows that claim it, in :data:`FLOW_SOURCES` order.
      suites (list[tuple[str, object, str]]): ``(flow, suite config,
        entries accessor)`` for the non-simulation flows, whose suites are
        not reachable from the ``verif/`` walk and must be emitted here.
      inputs (list[str]): every file read, for the input hashes.
      failures (list[str]): regression files that would not load.
    """

    by_suite: dict[str, list[str]] = dc_field(default_factory=dict)
    suites: list[tuple[str, object, str]] = dc_field(default_factory=list)
    inputs: list[str] = dc_field(default_factory=list)
    failures: list[str] = dc_field(default_factory=list)


def _collect_flows(project_root: Path) -> _Flows:
    """Read every flow regression file the project declares.

    Discovery per flow is root filename first (``<root>/<filename>``),
    then the flow's ``cfg-rtl-reg`` path from ``root_config.yaml`` — the
    precedence ``rb <flow>-regression`` applies when no ``-c`` is passed
    (local file, then configured path), so the graph has no private,
    stricter discovery rule (#389). Loading goes through each flow's own
    ``*RegConfig``, which is the same class ``rb <flow>-regression``
    constructs — so a suite this says is a CDC suite is one
    ``rb cdc-regression`` would actually run. A file that will not load
    is recorded and skipped: flow provenance is a labelling nicety, and
    losing it must not cost a project its whole graph.
    """
    flows = _Flows()
    root_cfg_path = project_root / "root_config.yaml"
    reg_paths = load_reg_cfg_paths(root_cfg_path)
    if root_cfg_path.is_file():
        # Wiring a manifest path into cfg-rtl-reg changes what the graph
        # discovers, so `rb graph build`'s no-op check has to see the edit.
        flows.inputs.append(str(root_cfg_path))
    for source in FLOW_SOURCES:
        path = project_root / source.filename
        if not path.is_file():
            configured = resolve_reg_cfg_path(reg_paths, root_cfg_path, source.flow)
            if configured is None or not os.path.isfile(configured):
                # Configured-but-missing is not a failure: reg-cfg-path
                # defaults to "regression.yaml" in every template, and a
                # project without one still has a valid (smaller) graph.
                continue
            path = Path(configured)
        flows.inputs.append(str(path))
        try:
            reg = source.loader(name=f"graph/{source.flow}", path=str(path))
        except Exception:
            log_event(
                logger,
                logging.WARNING,
                "graph_config.regression_load_failed",
                flow=source.flow,
                path=str(path),
            )
            flows.failures.append(_rel(project_root, path))
            continue
        for suite_cfg in reg.get_suite_configs():
            suite_path = suite_cfg.get_path()
            suite_rel = _rel(project_root, os.path.dirname(suite_path))
            claimed = flows.by_suite.setdefault(suite_rel, [])
            if source.flow not in claimed:
                claimed.append(source.flow)
            if source.entries:
                flows.inputs.append(suite_path)
                flows.suites.append((source.flow, suite_cfg, source.entries))
    return flows


def _flow_attr(by_suite: dict[str, list[str]], suite_rel: str) -> str | list[str]:
    """The ``flow`` stamp for one suite: a string, or a list when shared."""
    claimed = by_suite.get(suite_rel) or [DEFAULT_FLOW]
    return claimed[0] if len(claimed) == 1 else list(claimed)


# ---------------------------------------------------------------------------
# Golden models
# ---------------------------------------------------------------------------


def _block_dir_owner(cfg: SpecConfig, blocks: list[SpecBlock]) -> SpecBlock | None:
    """Pick the block a ``specs.yaml``'s sibling files belong to.

    Mirrors :func:`~rtl_buddy.tools.spec_trace.build_spec_to_models_map`:
    a single-block file owns its directory outright; in a multi-block
    file only a block named after the directory can claim it, because
    nothing else in the config says which block a loose ``.py`` serves.
    """
    if len(blocks) == 1:
        return blocks[0]
    dir_name = os.path.basename(os.path.dirname(cfg.get_path()))
    for block in blocks:
        if block.name == dir_name:
            return block
    return None


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > _MAX_SCAN_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _scan_verif_sources(verif_dir: str) -> list[tuple[str, str]]:
    """Return ``(abs path, text)`` for every readable verif source file."""
    sources: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(verif_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            if not name.endswith(_VERIF_SOURCE_SUFFIXES):
                continue
            path = Path(dirpath) / name
            text = _read_text(path)
            if text is not None:
                sources.append((os.path.abspath(str(path)), text))
    return sources


def _golden_model_files(spec_dir: str) -> list[str]:
    """Python files sitting next to a ``specs.yaml``, by convention golden models.

    Private modules (leading underscore, including ``__init__.py``) are
    shared plumbing rather than a model of the block, so they are skipped.
    """
    found = []
    for name in sorted(os.listdir(spec_dir)):
        if not name.endswith(".py") or name.startswith("_"):
            continue
        path = os.path.join(spec_dir, name)
        if os.path.isfile(path):
            found.append(path)
    return found


def _referencing_files(stem: str, sources: list[tuple[str, str]]) -> list[str]:
    """Absolute paths of verif sources naming ``stem`` as a whole word.

    Catches both ``from tiny_alu_model import ...`` (cocotb / preproc
    imports it after a ``sys.path`` insert) and a plain prose or plusarg
    mention of ``tiny_alu_model.py`` in a SystemVerilog testbench.
    """
    pattern = re.compile(rf"\b{re.escape(stem)}\b")
    return [path for path, text in sources if pattern.search(text)]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _add_spec_nodes(
    gb: _GraphBuilder,
    project_root: Path,
    spec_configs: list[SpecConfig],
    verif_sources: list[tuple[str, str]],
) -> dict[str, list[str]]:
    """Emit spec blocks, their docs, coverage items and golden models.

    Returns a map of coverage-item id -> owning block names. An id may be
    declared by more than one block; ``rb spec check-coverage`` matches
    ``covers:`` entries on the bare id across every block, and the graph
    reproduces that exactly (one ``covers`` edge per declaring block).
    """
    cov_owners: dict[str, list[str]] = {}

    for cfg in spec_configs:
        spec_path = cfg.get_path()
        spec_rel = _rel(project_root, spec_path)
        spec_dir = os.path.dirname(spec_path)
        blocks = cfg.get_blocks()

        for block in blocks:
            block_node = gb.add_node(
                spec_block_id(block.name),
                "spec_block",
                block.name,
                file=spec_rel,
                desc=block.desc,
            )

            for doc in block.docs:
                doc_abs = doc if os.path.isabs(doc) else os.path.join(spec_dir, doc)
                doc_rel = _rel(project_root, doc_abs)
                doc_node = gb.add_node(
                    spec_doc_id(doc_rel),
                    "spec_doc",
                    os.path.basename(doc_rel),
                    file=doc_rel,
                    exists=os.path.isfile(doc_abs),
                )
                gb.add_link(block_node, doc_node, "documented_by")

            for item in block.coverage_items:
                item_node = gb.add_node(
                    coverage_item_id(block.name, item.id),
                    "coverage_item",
                    item.id,
                    file=spec_rel,
                    desc=item.desc,
                    block=block.name,
                )
                # `declares` is the suite->test/testbench containment edge;
                # a block owning its coverage items is the same relation, so
                # the vocabulary is reused rather than widened.
                gb.add_link(block_node, item_node, "declares")
                cov_owners.setdefault(item.id, []).append(block.name)

        owner = _block_dir_owner(cfg, blocks)
        if owner is None:
            continue
        for golden_path in _golden_model_files(spec_dir):
            golden_rel = _rel(project_root, golden_path)
            stem = Path(golden_path).stem
            referenced_by = sorted(
                _rel(project_root, p) for p in _referencing_files(stem, verif_sources)
            )
            golden_node = gb.add_node(
                golden_model_id(golden_rel),
                "golden_model",
                stem,
                file=golden_rel,
                referenced_by=referenced_by,
            )
            gb.add_link(golden_node, spec_block_id(owner.name), "implements")

    return cov_owners


def _add_model_nodes(
    gb: _GraphBuilder,
    project_root: Path,
    spec_configs: list[SpecConfig],
    model_entries: list[tuple[str, object]],
) -> None:
    """Emit model nodes plus their spec and design-tier links."""
    for models_path, model in model_entries:
        _add_model_node(gb, project_root, models_path, model)

    # Reuse the exact mapping `rb spec check-design` reports, so a model
    # that command calls "covered" is the same one linked here.
    spec_to_models = build_spec_to_models_map(spec_configs, model_entries)
    for key, models in spec_to_models.items():
        _, _, block_name = key.rpartition("::")
        for models_path, model_name in models:
            gb.add_link(
                model_id(_rel(project_root, models_path), model_name),
                spec_block_id(block_name),
                "specified_by",
            )


def _add_model_node(
    gb: _GraphBuilder, project_root: Path, models_path: str, model
) -> str:
    """Emit one model node and its ``maps_to`` stitch to the design tier.

    A ``graph: false`` model (#479) still gets its node — spec and test
    cross-references point at it, and dropping it would break them — but
    no ``maps_to``: the whole content of the opt-out is that no module is
    named after this model, so an edge claiming otherwise would be false
    *and* would strand the model as a permanent dangling target in the
    merged graph. The flag rides on the node so a consumer can tell an
    opted-out model from one whose export merely has not run.
    """
    models_rel = _rel(project_root, models_path)
    node = gb.add_node(
        model_id(models_rel, model.name),
        "model",
        model.name,
        file=models_rel,
        desc=model.desc,
        graph=None if model.graph else False,
    )
    # The config↔design stitch. `module:<top>` is a design-tier id that
    # this tier does not define; it resolves when the tiers are merged,
    # and stays dangling (harmless — node-link readers auto-create it) if
    # only the config tier is exported.
    if model.graph:
        gb.add_link(node, module_id(model.get_top()), MAPS_TO)
    return node


def _testbench_kind(tb: TestbenchConfig) -> str:
    if tb.is_cocotb():
        return "cocotb"
    if tb.is_systemc():
        return "systemc"
    return "hdl"


def _add_testbench_node(
    gb: _GraphBuilder,
    suite_rel: str,
    suite_node: str,
    tests_rel: str,
    tb: TestbenchConfig,
    flow: str | list[str],
    opted_out_tops: frozenset[str] = frozenset(),
) -> str:
    node = gb.add_node(
        testbench_id(suite_rel, tb.get_name()),
        "testbench",
        tb.get_name(),
        file=tests_rel,
        toplevel=tb.toplevel,
        kind=_testbench_kind(tb),
        cocotb_modules=tb.cocotb.get_modules() if tb.is_cocotb() else None,
        # `kind` already says cocotb; `cocotb` is the flat boolean a
        # consumer can test on a test node and a testbench node alike,
        # without knowing which attribute each type spells it with.
        cocotb=True if tb.is_cocotb() else None,
        flow=flow,
    )
    gb.add_link(suite_node, node, "declares")
    # The testbench↔design stitch. Same relation as the model node's
    # `maps_to` and same target namespace, but its own verb: `toplevel:`
    # names the module the testbench *elaborates from*, so
    # `module:<toplevel>` is where this metadata node meets the
    # hierarchy `rb graph build` exports TB-rooted. Only emitted when
    # `toplevel:` is actually declared — a plain SV testbench that
    # leaves it out is topped by convention (the testbench's own name),
    # and guessing here would be inference in a tier that is pure
    # config readback. `rb graph build` adds that edge instead, from
    # the top the viewer really elaborated.
    #
    # `opted_out_tops` holds the roots of this suite's `graph: false`
    # models (#479). A cocotb/SystemC harness declares `toplevel: <the
    # DUT>`, so an opted-out DUT would otherwise leave the testbench
    # pointing at a `module:` node the design tier is never going to
    # export — the same permanently dangling stitch the model node's
    # `maps_to` is suppressed to avoid.
    if tb.toplevel and tb.toplevel not in opted_out_tops:
        gb.add_link(node, module_id(tb.toplevel), ELABORATES_AS)
    return node


def _declared_testbenches(path: str) -> list[TestbenchConfig] | None:
    """Every ``testbenches:`` entry in a suite, including unused ones.

    ``SuiteConfig`` only keeps testbenches that a test references, so a
    declared-but-unused testbench would be invisible. Re-reading through
    ``SuiteConfigFile`` (the same pyserde schema ``SuiteConfig`` uses —
    still no second parser) recovers them. Returns None if the file does
    not round-trip, in which case the caller falls back to the
    test-derived set.
    """
    try:
        with open(path, "r") as handle:
            return from_yaml(SuiteConfigFile, handle.read()).testbenches
    except Exception:
        return None


def _add_suite_nodes(
    gb: _GraphBuilder,
    project_root: Path,
    verif_dir: str,
    cov_owners: dict[str, list[str]],
    by_suite: dict[str, list[str]],
) -> list[str]:
    """Emit suites, testbenches, tests and everything hanging off them.

    Returns the repo-relative paths of suites that failed to load.
    """
    failures: list[str] = []

    for path in _walk_yaml_files(verif_dir, "tests.yaml"):
        suite_rel = _rel(project_root, os.path.dirname(path))
        try:
            suite = SuiteConfig(path)
        except FatalRtlBuddyError:
            log_event(
                logger,
                logging.WARNING,
                "graph_config.suite_load_failed",
                path=path,
            )
            failures.append(_rel(project_root, path))
            continue

        tests_rel = _rel(project_root, path)
        flow = _flow_attr(by_suite, suite_rel)
        suite_node = gb.add_node(
            suite_id(suite_rel),
            "suite",
            os.path.basename(suite_rel) or suite_rel,
            file=tests_rel,
            flow=flow,
        )

        # Roots of the models this suite's tests run against that opted
        # out of the design tier (#479). Computed before any testbench
        # node is emitted because the declared-but-unused pass runs
        # first and `add_link` keeps the first sighting of an edge.
        opted_out_tops = frozenset(
            test.get_model().get_top()
            for test in suite.get_tests()
            if not test.get_model().graph
        )

        declared = _declared_testbenches(path)
        if declared is not None:
            for tb in declared:
                _add_testbench_node(
                    gb, suite_rel, suite_node, tests_rel, tb, flow, opted_out_tops
                )

        for test in suite.get_tests():
            tb_node = _add_testbench_node(
                gb,
                suite_rel,
                suite_node,
                tests_rel,
                test.get_testbench(),
                flow,
                opted_out_tops,
            )
            model = test.get_model()
            model_node = _add_model_node(gb, project_root, model.path, model)

            test_node = gb.add_node(
                test_id(suite_rel, test.get_name()),
                "test",
                test.get_name(),
                file=tests_rel,
                desc=test.desc,
                # Raw `reglvl:` as written — an int, or the per-builder
                # dict. Resolving it needs a builder, which is a run-time
                # choice and has no place in a static graph.
                reglvl=test._reglvl,
                cocotb_modules=test.get_testbench().cocotb.get_modules()
                if test.get_testbench().is_cocotb()
                else None,
                cocotb=True if test.get_testbench().is_cocotb() else None,
                flow=flow,
                xfail=test.is_xfail() or None,
            )
            gb.add_link(suite_node, test_node, "declares")
            gb.add_link(test_node, tb_node, "runs_on")
            gb.add_link(tb_node, model_node, "exercises")

            for cov in test.covers or []:
                owners = cov_owners.get(cov)
                if not owners:
                    log_event(
                        logger,
                        logging.DEBUG,
                        "graph_config.unknown_coverage_item",
                        path=path,
                        test=test.get_name(),
                        item=cov,
                    )
                    continue
                for block_name in owners:
                    gb.add_link(
                        test_node,
                        coverage_item_id(block_name, cov),
                        "covers",
                    )

    return failures


def _add_flow_suite_nodes(
    gb: _GraphBuilder,
    project_root: Path,
    flows: _Flows,
    cov_owners: dict[str, list[str]],
) -> None:
    """Emit the non-simulation flows' suites and runs.

    A `synth.yaml` / `fpv.yaml` / `cdc.yaml` / `fpga.yaml` is a suite of
    named runs against a model, which is the same shape a `tests.yaml` has
    — so it reuses the same node types and the same ids (`suite:<dir>`,
    `test:<dir>#<name>`) rather than inventing a parallel vocabulary. Two
    things differ: there is no testbench between the run and the model, so
    `exercises` is emitted from the run itself; and a run's `top:` (which a
    formal verification may override away from the model name) is the
    run's own config->design stitch, `targets` — the same relation a
    testbench's `elaborates_as` states, from a third kind of source.

    `covers:` does not differ: an fpv run declaring one gets the same
    run -> coverage-item edges a simulation test gets, which is what lets
    a formal run reach the spec tier at all.
    """
    for flow, suite_cfg, entries_attr in flows.suites:
        cfg_path = suite_cfg.get_path()
        suite_rel = _rel(project_root, os.path.dirname(cfg_path))
        cfg_rel = _rel(project_root, cfg_path)
        stamp = _flow_attr(flows.by_suite, suite_rel)
        suite_node = gb.add_node(
            suite_id(suite_rel),
            "suite",
            os.path.basename(suite_rel) or suite_rel,
            file=cfg_rel,
            flow=stamp,
        )
        for entry in getattr(suite_cfg, entries_attr)():
            model = entry.get_model()
            model_node = _add_model_node(gb, project_root, model.path, model)
            top = entry.get_top()
            test_node = gb.add_node(
                test_id(suite_rel, entry.get_name()),
                "test",
                entry.get_name(),
                file=cfg_rel,
                desc=getattr(entry, "desc", None),
                # Raw `reglvl:` as written, exactly as a simulation test
                # node keeps it — resolving it needs a tool name.
                reglvl=getattr(entry, "_reglvl", None),
                tool=entry.get_tool_name(),
                toplevel=top,
                # Reduced-configuration formal runs (#359) elaborate the top
                # at overridden parameters — the run node says which, so a
                # reader can tell two runs of the same top apart. `getattr`
                # because only fpv entries carry the field.
                params=getattr(entry, "params", None) or None,
                flow=flow,
            )
            gb.add_link(suite_node, test_node, "declares")
            gb.add_link(test_node, model_node, "exercises")
            # A run whose top is the model's own root inherits the model's
            # `graph: false` opt-out (#479): the design tier will not export
            # that hierarchy, so declaring an edge into it would only add a
            # dangling target. A run that names its own checker top (fpv)
            # keeps its stitch — that top is the flow's, not the model's.
            if top and (model.graph or top != model.get_top()):
                gb.add_link(test_node, module_id(top), TARGETS)
            # An fpv run may declare `covers:` exactly as a test does
            # (rtl-buddy/rtl_buddy#385) — same field, same edge, same
            # fan-out to every declaring block. `getattr` because the
            # other flows' entries have no such field (yet); a flow that
            # grows one gets the edge for free.
            for cov in getattr(entry, "covers", None) or []:
                owners = cov_owners.get(cov)
                if not owners:
                    log_event(
                        logger,
                        logging.DEBUG,
                        "graph_config.unknown_coverage_item",
                        path=cfg_path,
                        test=entry.get_name(),
                        item=cov,
                    )
                    continue
                for block_name in owners:
                    gb.add_link(
                        test_node,
                        coverage_item_id(block_name, cov),
                        "covers",
                    )


def _hash_inputs(project_root: Path, paths: list[str]) -> list[dict]:
    """Content hashes of every config file the extraction read."""
    entries = []
    for path in sorted({os.path.realpath(p) for p in paths}):
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            digest = None
        entries.append({"path": _rel(project_root, path), "sha256": digest})
    return entries


def extract_config_tier(
    project_root: str | os.PathLike,
    *,
    spec_dir: str | os.PathLike | None = None,
    verif_dir: str | os.PathLike | None = None,
    design_dir: str | os.PathLike | None = None,
) -> ConfigTier:
    """Extract the config tier of the design knowledge graph.

    Args:
      project_root: Directory holding ``root_config.yaml``. All node ids
        are relative to it.
      spec_dir: Tree searched for ``specs.yaml``. Defaults to
        ``<project_root>/spec`` — the same default as ``rb spec check-coverage``.
      verif_dir: Tree searched for ``tests.yaml``. Defaults to ``<project_root>/verif``.
      design_dir: Tree searched for ``models.yaml``. Defaults to ``<project_root>/design``.

    Returns:
      ConfigTier: graph, provenance meta, and any suites that failed to load.

    A missing search directory is not an error — a project with no specs
    yet still has a valid (smaller) graph.
    """
    root = Path(os.path.realpath(str(project_root)))
    search_spec = str(spec_dir) if spec_dir is not None else str(root / "spec")
    search_verif = str(verif_dir) if verif_dir is not None else str(root / "verif")
    search_design = str(design_dir) if design_dir is not None else str(root / "design")

    spec_configs = (
        discover_spec_configs(search_spec) if os.path.isdir(search_spec) else []
    )
    model_entries = (
        discover_model_configs(search_design) if os.path.isdir(search_design) else []
    )
    verif_sources = (
        _scan_verif_sources(search_verif) if os.path.isdir(search_verif) else []
    )

    flows = _collect_flows(root)

    gb = _GraphBuilder()
    cov_owners = _add_spec_nodes(gb, root, spec_configs, verif_sources)
    _add_model_nodes(gb, root, spec_configs, model_entries)
    failures = (
        _add_suite_nodes(gb, root, search_verif, cov_owners, flows.by_suite)
        if os.path.isdir(search_verif)
        else []
    )
    _add_flow_suite_nodes(gb, root, flows, cov_owners)
    failures += flows.failures

    generator = {
        "tool": "rtl_buddy",
        "version": _tool_version(),
        "tier": CONFIG_TIER,
    }
    graph = {
        "directed": True,
        "multigraph": True,
        "graph": {
            "schema_version": SCHEMA_VERSION,
            "generator": generator,
            "project_root_rel": ".",
        },
        "nodes": gb.node_list(),
        "links": gb.link_list(),
    }

    inputs = [cfg.get_path() for cfg in spec_configs]
    inputs += [p for p, _ in model_entries]
    inputs += (
        _walk_yaml_files(search_verif, "tests.yaml")
        if os.path.isdir(search_verif)
        else []
    )
    # The regression files and the per-flow suites they list are inputs
    # now: wiring a suite into `fpv_regression.yaml` changes the graph, so
    # `rb graph build`'s no-op check has to see the edit.
    inputs += flows.inputs
    meta = {
        "schema_version": SCHEMA_VERSION,
        "tiers": {
            CONFIG_TIER: {
                "generator": generator,
                "inputs": _hash_inputs(root, inputs),
                "suite_load_failures": failures,
            }
        },
    }

    log_event(
        logger,
        logging.DEBUG,
        "graph_config.extracted",
        nodes=len(graph["nodes"]),
        links=len(graph["links"]),
        failures=len(failures),
    )
    return ConfigTier(graph=graph, meta=meta, suite_load_failures=failures)


def build_config_tier(project_root: str | os.PathLike, **kwargs) -> dict:
    """Config-tier ``graph.json`` payload for ``project_root``.

    Thin wrapper over :func:`extract_config_tier` for callers that only
    want the graph; keyword arguments are forwarded unchanged.
    """
    return extract_config_tier(project_root, **kwargs).graph


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def serialize_graph(graph: dict) -> str:
    """Render a graph (or meta) dict as canonical JSON text.

    Stable formatting — the extractor already sorts nodes and links — so
    a re-export with no config change is a byte-identical file and shows
    up as no diff.
    """
    return json.dumps(graph, ensure_ascii=True, indent=2, sort_keys=False) + "\n"


def _write_json(payload: dict, path: str | os.PathLike) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(serialize_graph(payload))
    os.replace(tmp, target)
    return target


def write_graph_json(graph: dict, path: str | os.PathLike) -> Path:
    """Write ``graph.json`` atomically, creating parent directories."""
    return _write_json(graph, path)


def write_graph_meta(meta: dict, path: str | os.PathLike) -> Path:
    """Write the ``graph-meta.json`` provenance sidecar atomically."""
    return _write_json(meta, path)
