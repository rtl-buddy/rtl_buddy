# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Config-tier extractor for the design knowledge graph.

Turns `tests.yaml`, `models.yaml`, `specs.yaml` and the repo-level regression files into
graph nodes and edges through the existing config loaders and
`rtl_buddy.tools.spec_trace`, so the graph agrees with `rb spec check-coverage` and `rb
spec check-design`. Every link is EXTRACTED and volatile run data stays out (see the
results overlay). Entry points are `build_config_tier` and `extract_config_tier`; the
envelope is documented in docs/concepts/graph.md.
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

# Bumped when the node/edge vocabulary changes incompatibly.
SCHEMA_VERSION = 1

# `generator.tier` of graphs this module produces.
CONFIG_TIER = "config"

# Defined in `tools.artifact_paths`, the bottom of the import graph; re-exported here
# for consumers.
from ..tools.artifact_paths import (  # noqa: E402
    GRAPH_JSON_NAME as GRAPH_JSON_NAME,
    GRAPH_META_NAME as GRAPH_META_NAME,
    run_artifact_root,
)

# Confidence tag for links. The config tier only reads config back, so every link is
# EXTRACTED.
EXTRACTED = "EXTRACTED"

# The three config-to-design stitches: one edge type per source kind, so consumers need
# not re-derive the kind from the source id prefix.
MAPS_TO = "maps_to"  #: model -> the module it names
ELABORATES_AS = "elaborates_as"  #: testbench -> the top it elaborates
TARGETS = "targets"  #: non-simulation run -> its ``top:``

# Text suffixes scanned for verif-side references to a golden model.
_VERIF_SOURCE_SUFFIXES = (".py", ".sv", ".svh", ".v", ".vh", ".yaml", ".yml", ".f")

# Directories skipped while scanning verif sources.
_SKIP_DIRS = frozenset(
    {".git", "__pycache__", "artefacts", "obj_dir", "node_modules", "venv", ".venv"}
)

# Largest verif source file read by the golden-model reference scan, in bytes.
_MAX_SCAN_BYTES = 1 << 20

# ---------------------------------------------------------------------------
# Flow provenance: repo-level regression files say which flow runs a suite, and
# `flow` becomes a node attribute.
# ---------------------------------------------------------------------------

# Simulation: `tests.yaml` suites under `verif/`, listed by `regression.yaml`.
FLOW_SIM = "sim"
# Synthesis: `synth.yaml` suites, listed by `synth_regression.yaml`.
FLOW_SYNTH = "synth"
# Formal property verification: `fpv.yaml` / `fpv_regression.yaml`.
FLOW_FPV = "fpv"
# CDC lint: `cdc.yaml` / `cdc_regression.yaml`.
FLOW_CDC = "cdc"
# FPGA implementation: `fpga.yaml` / `fpga_regression.yaml`.
FLOW_FPGA = "fpga"
# Style lint (verible): `lint.yaml` / `lint_regression.yaml`.
FLOW_LINT = "lint"

# Flow of a suite no regression file claims; it is still a simulation suite.
DEFAULT_FLOW = FLOW_SIM


@dataclass(frozen=True)
class _FlowSource:
    """One repo-level regression file and how to walk what it lists.

    `entries` is the accessor on each listed suite config that returns that flow's runs.
    The simulation flow leaves it empty because the `verif/` walk already emits its
    suites.
    """

    flow: str
    filename: str
    loader: type
    entries: str = ""


# Flow sources in read order. Discovery matches `rb <flow>-regression`: filename at the
# project root, then the `cfg-rtl-reg` path from `root_config.yaml`.
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
# Node ids: the merge keys across tiers, built only here. Paths are repo-relative
# and posix-separated.
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
    """Return the design-tier module id.

    The config tier never creates these nodes; it points its config-to-design stitches
    (`maps_to`, `elaborates_as`, `targets`) at them.
    """
    return f"module:{module_name}"


# ---------------------------------------------------------------------------
# Accumulator
# ---------------------------------------------------------------------------


@dataclass
class _GraphBuilder:
    """Collects nodes and links, de-duplicating by id and by whole link."""

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
            # Same id and type: fill in missing attributes, keep the first sighting's.
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
      graph: Node-link JSON, ready to write as `graph.json`.
    meta: Provenance sidecar for `graph-meta.json`: generator identity and the content
    hash of every config file read. Kept out of `graph` because hashes churn on every
    edit.
    suite_load_failures: Repo-relative `tests.yaml` paths that failed to load.
    Extraction is best-effort; callers decide whether to fail.
    """

    graph: dict
    meta: dict
    suite_load_failures: list[str] = dc_field(default_factory=list)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _rel(project_root: Path, path: str | os.PathLike) -> str:
    """Return a repo-relative posix path for use in node ids.

    Both sides go through `realpath`, so one file reached by two routes yields one id.
    Paths outside the project root are returned absolute.
    """
    resolved = Path(os.path.realpath(str(path)))
    root = Path(os.path.realpath(str(project_root)))
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def default_graph_dir(
    project_root: str | os.PathLike, run_tag: str | None = None
) -> Path:
    """Return `<project root>/artefacts/graph`.

    `run_tag` moves it into that run's namespace, where a concurrent regression writes
    its own `results-overlay.json`. `graph.json` is not per-run, so tagged callers still
    read it from the untagged directory.
    """
    return run_artifact_root(project_root, run_tag) / "graph"


# ---------------------------------------------------------------------------
# Flow discovery
# ---------------------------------------------------------------------------


@dataclass
class _Flows:
    """What the repo-level regression files said.

    Attributes:
    by_suite: Suite dir (repo-relative) -> the flows that claim it, in `FLOW_SOURCES`
    order.
    suites: `(flow, suite config, entries accessor)` for the non-simulation flows, whose
    suites the `verif/` walk cannot reach.
      inputs: Every file read, for the input hashes.
      failures: Regression files that would not load.
    """

    by_suite: dict[str, list[str]] = dc_field(default_factory=dict)
    suites: list[tuple[str, object, str]] = dc_field(default_factory=list)
    inputs: list[str] = dc_field(default_factory=list)
    failures: list[str] = dc_field(default_factory=list)


def _collect_flows(project_root: Path) -> _Flows:
    """Read every flow regression file the project declares.

    Discovery per flow is the root filename, then the `cfg-rtl-reg` path from
    `root_config.yaml`, the same precedence `rb <flow>-regression` uses without `-c`.
    Files load through each flow's own `*RegConfig`. A file that will not load is
    recorded and skipped, so flow labels never cost a project its graph.
    """
    flows = _Flows()
    root_cfg_path = project_root / "root_config.yaml"
    reg_paths = load_reg_cfg_paths(root_cfg_path)
    if root_cfg_path.is_file():
        # The `cfg-rtl-reg` path changes what is discovered, so the no-op check must see
        # it.
        flows.inputs.append(str(root_cfg_path))
    for source in FLOW_SOURCES:
        path = project_root / source.filename
        if not path.is_file():
            configured = resolve_reg_cfg_path(reg_paths, root_cfg_path, source.flow)
            if configured is None or not os.path.isfile(configured):
                # A configured but missing file is not a failure; `regression.yaml` is
                # the template default.
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
    """Return the `flow` stamp for one suite: a string, or a list when shared."""
    claimed = by_suite.get(suite_rel) or [DEFAULT_FLOW]
    return claimed[0] if len(claimed) == 1 else list(claimed)


# ---------------------------------------------------------------------------
# Golden models
# ---------------------------------------------------------------------------


def _block_dir_owner(cfg: SpecConfig, blocks: list[SpecBlock]) -> SpecBlock | None:
    """Pick the block a `specs.yaml`'s sibling files belong to.

    Mirrors `spec_trace.build_spec_to_models_map`: a single-block file owns its
    directory; in a multi-block file only a block named after the directory can claim
    it.
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
    """Return `(abs path, text)` for every readable verif source file."""
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
    """Return the Python files next to a `specs.yaml`, by convention golden models.

    Private modules (leading underscore, including `__init__.py`) are skipped.
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
    """Return absolute paths of verif sources naming `stem` as a whole word.

    Matches both imports (after a `sys.path` insert) and prose or plusarg mentions of
    `<stem>.py`.
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

    Returns a map of coverage-item id -> owning block names. An id declared by several
    blocks gets one `covers` edge per block, as `rb spec check-coverage` matches
    `covers:` on the bare id.
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
                # `declares` is the containment edge; a block owning its coverage items
                # reuses it.
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


def _export_key(model) -> tuple[str, str]:
    """Return a model's identity: the realpath of its `models.yaml` plus its `name:`.

    Matches `graph.build._model_key`, and avoids the `model:` node id so both sides make
    paths relative the same way.
    """
    return (os.path.realpath(model.path) if model.path else "", model.name)


def _exports_design(model, exported: frozenset[tuple[str, str]] | None) -> bool:
    """Return True when this build's design tier will export `model`'s hierarchy.

    It will not when the model declared `graph: false`, or when `--model` or `-c`
    narrowed the design tier past it while the config tier still walks all of
    `--design-dir`. A stitch to a module nobody defines dangles, or resolves against
    another model's hierarchy when their `top:` matches. `exported=None` means no
    selection to respect (`--no-design` or a direct caller), so stitches are kept.
    """
    if not model.graph:
        return False
    return exported is None or _export_key(model) in exported


def _add_model_nodes(
    gb: _GraphBuilder,
    project_root: Path,
    spec_configs: list[SpecConfig],
    model_entries: list[tuple[str, object]],
    exported: frozenset[tuple[str, str]] | None = None,
) -> None:
    """Emit model nodes plus their spec and design-tier links."""
    for models_path, model in model_entries:
        _add_model_node(gb, project_root, models_path, model, exported)

    # Reuse the mapping `rb spec check-design` reports, so both agree on what is
    # covered.
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
    gb: _GraphBuilder,
    project_root: Path,
    models_path: str,
    model,
    exported: frozenset[tuple[str, str]] | None = None,
) -> str:
    """Emit one model node and its `maps_to` stitch to the design tier.

    A model the design tier will not export keeps its node, since spec and test edges
    point at it, but gets no `maps_to`; see `_exports_design`. The `graph:` flag is
    stored on the node so consumers can tell an opted-out model from one outside this
    build's selection.
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
    # `module:<top>` is a design-tier id; it resolves at merge and stays dangling in a
    # config-only export.
    if _exports_design(model, exported):
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
    unexported: bool = False,
) -> str:
    node = gb.add_node(
        testbench_id(suite_rel, tb.get_name()),
        "testbench",
        tb.get_name(),
        file=tests_rel,
        toplevel=tb.toplevel,
        kind=_testbench_kind(tb),
        cocotb_modules=tb.cocotb.get_modules() if tb.is_cocotb() else None,
        # `cocotb` is a flat boolean testable on test and testbench nodes alike.
        cocotb=True if tb.is_cocotb() else None,
        flow=flow,
    )
    gb.add_link(suite_node, node, "declares")
    # The testbench-to-design stitch, emitted only when `toplevel:` is declared;
    # guessing the top of a plain SV testbench would be inference, and `rb graph build`
    # adds that edge from the elaborated top. `unexported` is true when no test on this
    # testbench runs against a model this build exports; the edge is then skipped,
    # because the design tier drops such a testbench export. The decision is per
    # testbench, not per top name, since models can share a root module.
    if tb.toplevel and not unexported:
        gb.add_link(node, module_id(tb.toplevel), ELABORATES_AS)
    return node


def _declared_testbenches(path: str) -> list[TestbenchConfig] | None:
    """Return every `testbenches:` entry in a suite, including unused ones.

    `SuiteConfig` keeps only referenced testbenches, so this re-reads through
    `SuiteConfigFile`. Returns None if the file does not round-trip; the caller then
    uses the test-derived set.
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
    exported: frozenset[tuple[str, str]] | None = None,
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

        # Testbenches none of whose tests run against an exported model, keyed by name.
        # Computed before any testbench node is emitted because the declared-but-unused
        # pass below runs first and `add_link` keeps the first sighting. A
        # declared-but-unused testbench keeps whatever it declares.
        tb_models: dict[str, list] = {}
        for test in suite.get_tests():
            tb_models.setdefault(test.get_testbench().get_name(), []).append(
                test.get_model()
            )
        unexported_tbs = frozenset(
            name
            for name, tb_dut in tb_models.items()
            if all(not _exports_design(model, exported) for model in tb_dut)
        )

        declared = _declared_testbenches(path)
        if declared is not None:
            for tb in declared:
                _add_testbench_node(
                    gb,
                    suite_rel,
                    suite_node,
                    tests_rel,
                    tb,
                    flow,
                    tb.get_name() in unexported_tbs,
                )

        for test in suite.get_tests():
            tb_node = _add_testbench_node(
                gb,
                suite_rel,
                suite_node,
                tests_rel,
                test.get_testbench(),
                flow,
                test.get_testbench().get_name() in unexported_tbs,
            )
            model = test.get_model()
            model_node = _add_model_node(gb, project_root, model.path, model, exported)

            test_node = gb.add_node(
                test_id(suite_rel, test.get_name()),
                "test",
                test.get_name(),
                file=tests_rel,
                desc=test.desc,
                # Raw `reglvl:` as written; resolving it needs a builder, a run-time
                # choice.
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
    exported: frozenset[tuple[str, str]] | None = None,
) -> None:
    """Emit the non-simulation flows' suites and runs.

    A `synth.yaml`, `fpv.yaml`, `cdc.yaml` or `fpga.yaml` suite has the same shape as
    `tests.yaml`, so it reuses the node types and ids (`suite:<dir>`,
    `test:<dir>#<name>`). There is no testbench, so `exercises` is emitted from the run,
    and the run's `top:` gives its `targets` stitch. A `covers:` on an fpv run yields
    the same run-to-coverage-item edges as a simulation test.
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
            model_node = _add_model_node(gb, project_root, model.path, model, exported)
            top = entry.get_top()
            test_node = gb.add_node(
                test_id(suite_rel, entry.get_name()),
                "test",
                entry.get_name(),
                file=cfg_rel,
                desc=getattr(entry, "desc", None),
                # Raw `reglvl:` as written, as on a simulation test.
                reglvl=getattr(entry, "_reglvl", None),
                tool=entry.get_tool_name(),
                toplevel=top,
                # Parameter overrides of reduced-configuration formal runs tell runs of
                # the same top apart. `getattr` because only fpv entries have the field.
                params=getattr(entry, "params", None) or None,
                flow=flow,
            )
            gb.add_link(suite_node, test_node, "declares")
            gb.add_link(test_node, model_node, "exercises")
            # Runs over a model this build does not export get neither stitch, whether
            # they top at the model's root or at their own checker.
            if top and _exports_design(model, exported):
                gb.add_link(test_node, module_id(top), TARGETS)
            # fpv runs may declare `covers:`. `getattr` because other flows' entries
            # lack the field.
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
    """Return content hashes of every config file the extraction read."""
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
    exported_models: list | None = None,
) -> ConfigTier:
    """Extract the config tier of the design knowledge graph.

    Args:
      project_root: Directory holding `root_config.yaml`; node ids are relative to it.
      spec_dir: Tree searched for `specs.yaml`. Defaults to `<project_root>/spec`.
      verif_dir: Tree searched for `tests.yaml`. Defaults to `<project_root>/verif`.
      design_dir: Tree searched for `models.yaml`. Defaults to `<project_root>/design`.
    exported_models: Models the design tier will export. Config-to-design stitches are
    emitted only for these. `None` means no selection, and every graphable model is
    stitched.

    Returns:
    The graph, provenance meta and any suites that failed to load. A missing search
    directory is not an error.
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
    exported = (
        None
        if exported_models is None
        else frozenset(_export_key(m) for m in exported_models)
    )

    gb = _GraphBuilder()
    cov_owners = _add_spec_nodes(gb, root, spec_configs, verif_sources)
    _add_model_nodes(gb, root, spec_configs, model_entries, exported)
    failures = (
        _add_suite_nodes(gb, root, search_verif, cov_owners, flows.by_suite, exported)
        if os.path.isdir(search_verif)
        else []
    )
    _add_flow_suite_nodes(gb, root, flows, cov_owners, exported)
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
    # Regression files and their per-flow suites are inputs; the no-op check must see
    # edits to them.
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
    """Return the config-tier `graph.json` payload for `project_root`.

    Wraps `extract_config_tier`; keyword arguments are forwarded.
    """
    return extract_config_tier(project_root, **kwargs).graph


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def serialize_graph(graph: dict) -> str:
    """Render a graph or meta dict as canonical JSON text.

    Formatting is stable, so an unchanged config re-exports byte-identically.
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
    """Write `graph.json` atomically, creating parent directories."""
    return _write_json(graph, path)


def write_graph_meta(meta: dict, path: str | os.PathLike) -> Path:
    """Write the `graph-meta.json` sidecar atomically."""
    return _write_json(meta, path)
