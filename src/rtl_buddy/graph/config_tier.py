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

Everything here reads through the existing loaders
(:mod:`rtl_buddy.config.spec`, :mod:`~rtl_buddy.config.model`,
:mod:`~rtl_buddy.config.suite`) and the discovery helpers in
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

from ..config.spec import SpecBlock, SpecConfig
from ..config.suite import SuiteConfig, SuiteConfigFile
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

GRAPH_JSON_NAME = "graph.json"
GRAPH_META_NAME = "graph-meta.json"

#: Confidence tag for links. The config tier is pure config readback, so
#: every link it emits is EXTRACTED — INFERRED/AMBIGUOUS are reserved for
#: the binding tier's ``dut.<signal>`` scan (#378).
EXTRACTED = "EXTRACTED"

#: Suffixes scanned when looking for verif-side references to a golden
#: model. Text formats only; anything else is skipped unread.
_VERIF_SOURCE_SUFFIXES = (".py", ".sv", ".svh", ".v", ".vh", ".yaml", ".yml", ".f")

#: Directories never descended into while scanning verif sources.
_SKIP_DIRS = frozenset(
    {".git", "__pycache__", "artefacts", "obj_dir", "node_modules", "venv", ".venv"}
)

#: Largest verif source file read during the golden-model reference scan.
_MAX_SCAN_BYTES = 1 << 20


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
    owns them. It only points ``maps_to`` links at them, and the shared
    id is what stitches the two tiers together at merge time.
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
    """Emit one model node and its ``maps_to`` stitch to the design tier."""
    models_rel = _rel(project_root, models_path)
    node = gb.add_node(
        model_id(models_rel, model.name),
        "model",
        model.name,
        file=models_rel,
        desc=model.desc,
    )
    # The config↔design stitch. `module:<name>` is a design-tier id that
    # this tier does not define; it resolves when the tiers are merged,
    # and stays dangling (harmless — node-link readers auto-create it) if
    # only the config tier is exported.
    gb.add_link(node, module_id(model.name), "maps_to")
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
) -> str:
    node = gb.add_node(
        testbench_id(suite_rel, tb.get_name()),
        "testbench",
        tb.get_name(),
        file=tests_rel,
        toplevel=tb.toplevel,
        kind=_testbench_kind(tb),
        cocotb_modules=tb.cocotb.get_modules() if tb.is_cocotb() else None,
    )
    gb.add_link(suite_node, node, "declares")
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
        suite_node = gb.add_node(
            suite_id(suite_rel),
            "suite",
            os.path.basename(suite_rel) or suite_rel,
            file=tests_rel,
        )

        declared = _declared_testbenches(path)
        if declared is not None:
            for tb in declared:
                _add_testbench_node(gb, suite_rel, suite_node, tests_rel, tb)

        for test in suite.get_tests():
            tb_node = _add_testbench_node(
                gb, suite_rel, suite_node, tests_rel, test.get_testbench()
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

    gb = _GraphBuilder()
    cov_owners = _add_spec_nodes(gb, root, spec_configs, verif_sources)
    _add_model_nodes(gb, root, spec_configs, model_entries)
    failures = (
        _add_suite_nodes(gb, root, search_verif, cov_owners)
        if os.path.isdir(search_verif)
        else []
    )

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
