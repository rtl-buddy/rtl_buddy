# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Post-merge binding stage for the design knowledge graph (#378).

The design tier knows the DUT hierarchy. The config tier knows which
test runs on which testbench. Neither knows that
``verif/demo_tiny_alu_cocotb/test_alu_random.py`` pokes
``dut.a`` — and that is exactly the boundary the highest-value agent
queries cross ("which tests drive this port?", "which golden model does
this test check against?").

This module closes it. It runs **after** the tiers are merged, because
it needs both halves at once: the config tier's ``test`` /
``testbench`` nodes tell it which Python module belongs to which
toplevel, and the design tier's ``port:<top>.<name>`` nodes are what a
``dut.<name>`` access has to resolve against.

Four kinds of edge come out of it:

``binds_to``   test -> Python module, and Python module -> DUT
               ``module:`` node. Both straight out of ``tests.yaml``
               (``cocotb: {module: M}`` and ``toplevel:``), so both
               EXTRACTED.
``imports``    Python module -> Python module, from a real ``ast``
               parse of the import statements. This is what makes a
               helper such as ``_alu_common.py`` reachable from the
               test that uses it.
``drives``     Python module -> ``port:``, from a ``dut.<name>``
               attribute scan. EXTRACTED when ``<name>`` is a port of
               the toplevel, INFERRED when it is not (a bus wrapper, an
               internal signal, or a design tier that was not exported
               so no port is known).
``checks_against``  test -> ``golden_model``, when the cocotb module
               imports a golden model — directly or through a helper.
``implemented_by``  ``dpi_function`` -> the C/C++/Python source under
               ``verif/`` or ``spec/`` that defines its C symbol
               (rtl-buddy-sch 127). EXTRACTED only for a *definition
               site* — a header's prototype and a caller both mention
               the symbol, and neither implements it — so a mention
               is INFERRED with ``resolved: false``, on the same
               evidence ladder ``drives`` uses. This is the DPI leg of
               the golden-model loop, alongside the cocotb
               ``checks_against`` path. Graphs from extractors that
               predate ``dpi_function`` nodes simply have none, and
               the pass is a silent no-op — no version coupling.

Two properties are load-bearing:

* **It works without the extractor.** When one is installed its Python
  nodes are reused (matched on the node's repo-relative ``file``), so
  the two never emit competing ids for one file. When it is absent this
  module synthesizes minimal ``py:<repo-rel path>`` nodes, which is what
  keeps ``rb graph build`` useful on a machine that has never heard of
  the extractor.
* **Reach is transitive but honest.** ``test_alu_random.py`` never says
  ``dut.a``; ``_alu_common.py`` does, and the test imports it. The
  ``drives`` edge is emitted from *both* Python modules, but the
  transitive one carries ``via`` naming the file the access really came
  from, so a consumer can tell first-hand evidence from inherited.
"""

from __future__ import annotations

import ast
import logging
import os
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from ..logging_utils import log_event

logger = logging.getLogger(__name__)

#: ``generator.tier`` / node ``tier`` stamped on everything this stage
#: emits. It is the same tier the extractor contributes to — the binding tier
#: has two producers, an optional external one and this one.
BINDING_TIER = "binding"

#: Node type and id prefix for a synthesized Python module node. Used
#: only when no existing node (the extractor's, typically) already claims the
#: file; see :func:`_existing_python_nodes`.
PYTHON_MODULE_TYPE = "python_module"
PY_NODE_PREFIX = "py:"

#: Node type + id prefix synthesized for a non-Python source file a DPI
#: symbol resolves to. Same claim-by-``file`` hand-off rule as
#: :data:`PYTHON_MODULE_TYPE`.
SOURCE_FILE_TYPE = "source_file"
SRC_NODE_PREFIX = "src:"

#: The design tier's node type for one ``import "DPI-C"`` /
#: ``export "DPI-C"`` item (rtl-buddy-sch 127). Emitted by
#: rtl-buddy-sch newer than v0.5.0; absent from older graphs, which is
#: the norm this stage degrades gracefully to.
DPI_FUNCTION_TYPE = "dpi_function"

BINDS_TO = "binds_to"
DRIVES = "drives"
CHECKS_AGAINST = "checks_against"
IMPORTS = "imports"
IMPLEMENTED_BY = "implemented_by"

EXTRACTED = "EXTRACTED"
INFERRED = "INFERRED"

BUILT = "built"
SKIPPED = "skipped"

#: The cocotb DUT handle is ``dut`` by overwhelming convention, and it is
#: also the name helpers take it under (``async def drive(dut, ...)``).
#: A ``@cocotb.test()`` function's first parameter is added to this set
#: per file, so a suite that calls it ``alu`` still binds.
DEFAULT_HANDLE = "dut"

#: Attributes of a cocotb handle that are the *handle API*, not a signal.
#: Everything starting with ``_`` is dropped too.
_HANDLE_API_ATTRS = frozenset(
    {"value", "setimmediatevalue", "get", "keys", "items", "log", "range"}
)

#: Guards against an import cycle or a pathological helper chain.
_MAX_IMPORT_DEPTH = 8

#: Largest Python file read during the scan.
_MAX_SCAN_BYTES = 1 << 20

#: Directories never descended into when collecting stage inputs.
_SKIP_DIRS = frozenset(
    {".git", "__pycache__", "artefacts", "obj_dir", "node_modules", "venv", ".venv"}
)

_DUT_ACCESS_RE = re.compile(r"\bdut\.([A-Za-z_]\w*)")
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.MULTILINE)

#: Non-Python suffixes the DPI symbol scan reads. C/C++ because that is
#: what DPI links against; Python is already collected for the cocotb
#: pass and is scanned too (a ctypes/cffi-backed model defines the
#: symbol's Python side).
_C_SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp")


# ---------------------------------------------------------------------------
# Source scanning
# ---------------------------------------------------------------------------


@dataclass
class PyScan:
    """What one Python file contributes to the binding stage.

    Attributes:
      path (Path): Absolute path scanned.
      accesses (dict[str, int]): ``dut.<name>`` -> first line it appears on.
      imports (list[str]): Module names imported, in source order.
      parsed (bool): False when the file did not parse and the regex
        fallback was used instead.
    """

    path: Path
    accesses: dict[str, int] = dc_field(default_factory=dict)
    imports: list[str] = dc_field(default_factory=list)
    parsed: bool = True


def _decorator_name(node: ast.expr) -> str:
    """Dotted name of a decorator expression (``cocotb.test()`` -> ``cocotb.test``)."""
    if isinstance(node, ast.Call):
        node = node.func
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _handle_names(tree: ast.AST) -> set[str]:
    """DUT-handle parameter names in a module.

    ``dut`` always counts. On top of that, the first parameter of every
    ``@cocotb.test()`` function counts, because that is the DUT handle
    whatever the author called it.
    """
    handles = {DEFAULT_HANDLE}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = [_decorator_name(d) for d in node.decorator_list]
        if not any(name.split(".")[-1] == "test" for name in names):
            continue
        args = node.args.posonlyargs + node.args.args
        if args:
            handles.add(args[0].arg)
    return handles


def _is_signal_attr(name: str) -> bool:
    return not name.startswith("_") and name not in _HANDLE_API_ATTRS


def scan_python_source(path: str | os.PathLike, text: str | None = None) -> PyScan:
    """Scan one Python file for DUT accesses and imports.

    Uses :mod:`ast`, so ``dut.a`` is distinguished from the string
    ``"dut.a"`` and from ``self.dut.a``, and the reported line is the
    real one. A file that does not parse (a syntax error, a Python
    version this interpreter predates) falls back to a regex sweep
    rather than contributing nothing — the stage is best-effort by
    charter.
    """
    target = Path(path)
    scan = PyScan(path=target)
    body = text if text is not None else _read_text(target)
    if body is None:
        return scan
    try:
        tree = ast.parse(body, filename=str(target))
    except (SyntaxError, ValueError):
        scan.parsed = False
        for match in _DUT_ACCESS_RE.finditer(body):
            name = match.group(1)
            if _is_signal_attr(name):
                line = body.count("\n", 0, match.start()) + 1
                scan.accesses.setdefault(name, line)
        scan.imports = list(dict.fromkeys(_IMPORT_RE.findall(body)))
        return scan

    handles = _handle_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            value = node.value
            if isinstance(value, ast.Name) and value.id in handles:
                if _is_signal_attr(node.attr):
                    scan.accesses.setdefault(node.attr, node.lineno)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                scan.imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # Relative imports need a package context a cocotb suite
            # does not have (cocotb puts the suite dir on sys.path and
            # imports the module flat), so only absolute ones resolve.
            if not node.level and node.module:
                scan.imports.append(node.module)
    scan.imports = list(dict.fromkeys(scan.imports))
    return scan


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > _MAX_SCAN_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def collect_sources(
    verif_dir: str | os.PathLike | None, spec_dir: str | os.PathLike | None
) -> list[str]:
    """Absolute paths of every source file the stage may read.

    Python for the cocotb pass, plus C/C++ for the DPI symbol scan
    (rtl-buddy-sch 127). A superset of what any single build actually
    parses (only modules reachable from a ``cocotb:`` entry are), and
    deliberately so: it is the fingerprint input list, and a file
    becoming reachable must invalidate the cache just as much as an
    edit to one already read.
    """
    suffixes = (".py",) + _C_SOURCE_SUFFIXES
    found: list[str] = []
    for root in (verif_dir, spec_dir):
        if root is None or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for name in sorted(filenames):
                if name.endswith(suffixes):
                    found.append(os.path.abspath(os.path.join(dirpath, name)))
    return sorted(set(found))


# ---------------------------------------------------------------------------
# Merged-graph index
# ---------------------------------------------------------------------------


def _rel(project_root: Path, path: str | os.PathLike) -> str:
    resolved = Path(os.path.realpath(str(path)))
    try:
        return resolved.relative_to(project_root).as_posix()
    except ValueError:
        return resolved.as_posix()


@dataclass
class _Index:
    """Everything the stage needs to look up in the merged graph."""

    nodes: dict[str, dict] = dc_field(default_factory=dict)
    #: ``module name -> {port names}``, from the design tier's ``port:`` nodes.
    ports: dict[str, set[str]] = dc_field(default_factory=dict)
    #: Toplevel of each testbench node id.
    toplevel: dict[str, str] = dc_field(default_factory=dict)
    #: ``test node id -> testbench node id``, from ``runs_on``.
    runs_on: dict[str, str] = dc_field(default_factory=dict)
    #: ``golden model stem -> node id``.
    golden: dict[str, str] = dc_field(default_factory=dict)
    #: ``repo-relative .py path -> existing node id`` (the extractor's, when present).
    python_nodes: dict[str, str] = dc_field(default_factory=dict)
    #: ``repo-relative path -> golden_model node id`` — the preferred
    #: ``implemented_by`` target when a DPI symbol resolves to a file
    #: the config tier already models (that IS the golden-model loop).
    golden_files: dict[str, str] = dc_field(default_factory=dict)
    #: ``dpi_function`` nodes from the design tier, in id order.
    dpi: list[dict] = dc_field(default_factory=list)


def _existing_python_nodes(nodes: list[dict]) -> dict[str, str]:
    """Node ids already claiming a ``.py`` file, keyed by that file.

    This is the extractor hand-off. The extractor names its Python
    nodes however it likes; what both tools agree on is the
    repo-relative path in ``file``, so that is the key. ``golden_model`` and ``spec_doc``
    nodes are excluded — they are config-tier nodes about a file, not a
    node *of* the Python module.
    """
    found: dict[str, str] = {}
    for node in nodes:
        path = node.get("file")
        node_id = node.get("id")
        if not path or not node_id or not str(path).endswith(".py"):
            continue
        if node.get("type") in ("golden_model", "spec_doc"):
            continue
        found.setdefault(str(path), node_id)
    return found


def _index_graph(merged: dict) -> _Index:
    index = _Index()
    nodes = merged.get("nodes") or []
    for node in nodes:
        node_id = node.get("id")
        if not node_id:
            continue
        index.nodes[node_id] = node
        node_type = node.get("type")
        if node_type == "port" and node_id.startswith("port:"):
            owner, _, port = node_id[len("port:") :].rpartition(".")
            if owner and port:
                index.ports.setdefault(owner, set()).add(port)
        elif node_type == "testbench":
            top = node.get("toplevel")
            if top:
                index.toplevel[node_id] = top
        elif node_type == "golden_model":
            label = node.get("label") or Path(str(node.get("file") or "")).stem
            if label:
                index.golden.setdefault(label, node_id)
            path = node.get("file")
            if path:
                index.golden_files.setdefault(str(path), node_id)
        elif node_type == DPI_FUNCTION_TYPE:
            index.dpi.append(node)
    for link in merged.get("links") or []:
        if link.get("type") == "runs_on":
            source, target = link.get("source"), link.get("target")
            if source and target:
                index.runs_on.setdefault(source, target)
    index.python_nodes = _existing_python_nodes(nodes)
    return index


# ---------------------------------------------------------------------------
# Accumulator
# ---------------------------------------------------------------------------


@dataclass
class _Builder:
    nodes: dict[str, dict] = dc_field(default_factory=dict)
    links: dict[tuple[str, str, str], dict] = dc_field(default_factory=dict)

    def add_node(self, node_id: str, node_type: str, label: str, **attrs) -> str:
        clean = {k: v for k, v in attrs.items() if v is not None}
        existing = self.nodes.get(node_id)
        if existing is not None:
            for key, value in clean.items():
                existing.setdefault(key, value)
            return node_id
        self.nodes[node_id] = {
            "id": node_id,
            "type": node_type,
            "label": label,
            "tier": BINDING_TIER,
            **clean,
        }
        return node_id

    def add_link(
        self,
        source: str,
        target: str,
        link_type: str,
        confidence: str = EXTRACTED,
        **attrs,
    ) -> bool:
        """Add a link, first sighting winning.

        De-duplication is on ``(source, target, type)``, not on the whole
        link the way :func:`rtl_buddy.graph.merge.merge_graphs` does it.
        The difference matters here: a cocotb module that imports a
        golden model *and* imports a helper that imports the same golden
        model has one relationship, not two, and the walk is
        breadth-first so the first sighting is the most direct evidence.
        Returns True when the link was new.
        """
        key = (source, target, link_type)
        if key in self.links:
            return False
        self.links[key] = {
            "source": source,
            "target": target,
            "type": link_type,
            "confidence": confidence,
            **{k: v for k, v in attrs.items() if v is not None},
        }
        return True

    def node_list(self) -> list[dict]:
        return [self.nodes[k] for k in sorted(self.nodes)]

    def link_list(self) -> list[dict]:
        return [self.links[k] for k in sorted(self.links)]


@dataclass
class BindingStage:
    """Result of one binding pass.

    Attributes:
      graph (dict): Node-link payload holding *only* this stage's nodes
        and links, ready to be merged into the graph it was computed
        from (and written to ``artefacts/graph/bind/graph.json``).
      status (str): ``built`` or ``skipped``.
      detail (str | None): Why, when ``skipped``.
      tests (int): cocotb tests that got a ``binds_to`` edge.
      modules (int): Python module nodes touched.
      reused_ids (int): Of those, how many reused an id another tier
        (the extractor) had already given the file.
      drives (int): ``drives`` edges emitted.
      extracted (int): Of those, how many matched a port exactly.
      inferred (int): The rest.
      checks (int): ``checks_against`` edges emitted.
      dpi_functions (int): ``dpi_function`` import nodes the DPI pass
        looked for an implementation of.
      dpi_implemented (int): ``implemented_by`` edges emitted.
      unresolved (list[dict]): cocotb modules whose file was not found,
        ``dut.<name>`` accesses that matched no port, and DPI symbols
        no source defined.
    """

    graph: dict
    status: str = SKIPPED
    detail: str | None = None
    tests: int = 0
    modules: int = 0
    reused_ids: int = 0
    drives: int = 0
    extracted: int = 0
    inferred: int = 0
    checks: int = 0
    dpi_functions: int = 0
    dpi_implemented: int = 0
    unresolved: list[dict] = dc_field(default_factory=list)

    @property
    def nodes(self) -> int:
        return len(self.graph.get("nodes") or [])

    @property
    def links(self) -> int:
        return len(self.graph.get("links") or [])

    def summary(self) -> dict:
        block: dict = {
            "status": self.status,
            "nodes": self.nodes,
            "links": self.links,
            "tests": self.tests,
            "python_modules": self.modules,
            "reused_node_ids": self.reused_ids,
            "drives": self.drives,
            "drives_extracted": self.extracted,
            "drives_inferred": self.inferred,
            "checks_against": self.checks,
            "dpi_functions": self.dpi_functions,
            "implemented_by": self.dpi_implemented,
        }
        if self.detail:
            block["detail"] = self.detail
        if self.unresolved:
            # Bounded: a suite with a typo'd handle must not blow up the
            # sidecar.
            block["unresolved"] = self.unresolved[:50]
        return block


def _empty_graph(generator: dict) -> dict:
    return {
        "directed": True,
        "multigraph": True,
        "graph": {
            "schema_version": 1,
            "generator": generator,
            "project_root_rel": ".",
        },
        "nodes": [],
        "links": [],
    }


# ---------------------------------------------------------------------------
# Module resolution
# ---------------------------------------------------------------------------


def resolve_module_file(name: str, search_dirs: list[Path]) -> Path | None:
    """File backing the import ``name``, searched the way cocotb sees it.

    ``rb test`` puts the suite directory on ``PYTHONPATH`` and hands
    cocotb a flat module name, so the suite dir is the first search root
    and the importing file's own directory the second. Both the module
    (``x.py``) and package (``x/__init__.py``) forms resolve.
    """
    parts = name.split(".")
    for base in search_dirs:
        candidate = base.joinpath(*parts).with_suffix(".py")
        if candidate.is_file():
            return candidate
        package = base.joinpath(*parts) / "__init__.py"
        if package.is_file():
            return package
    return None


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def bind_python(
    merged: dict,
    project_root: str | os.PathLike,
    *,
    generator: dict | None = None,
    verif_dir: str | os.PathLike | None = None,
    spec_dir: str | os.PathLike | None = None,
) -> BindingStage:
    """Bind cocotb Python and DPI C symbols to the hierarchy in ``merged``.

    Args:
      merged: The merged graph, *after* the design and config tiers are
        unioned. Read-only — the stage's contribution comes back in
        :attr:`BindingStage.graph` for the caller to merge in, so the
        pass stays a pure function of its input.
      project_root: Directory holding ``root_config.yaml``. Every path in
        a node id is relative to it.
      generator: ``graph.generator`` block for the emitted graph.
      verif_dir / spec_dir: Roots the DPI symbol scan reads C/C++/Python
        sources from. Default to ``<project_root>/verif`` and
        ``<project_root>/spec``.

    Returns:
      BindingStage: the contribution plus per-edge-class counts.

    Never raises: an unparseable helper, a missing cocotb module, a
    ``dut.<name>`` matching no port, a DPI symbol nothing defines are
    all recorded and the rest of the pass continues.

    A graph without ``dpi_function`` nodes — anything exported by
    rtl-buddy-sch v0.5.0 or older — simply runs the cocotb pass alone;
    the DPI stitch requires no particular extractor version.
    """
    root = Path(os.path.realpath(str(project_root)))
    gen = generator or {"tool": "rtl_buddy", "tier": BINDING_TIER}
    index = _index_graph(merged)

    cocotb_tests = [
        node
        for node in (merged.get("nodes") or [])
        if node.get("type") == "test" and node.get("cocotb_modules")
    ]
    if not cocotb_tests and not index.dpi:
        return BindingStage(
            graph=_empty_graph(gen),
            status=SKIPPED,
            detail="no cocotb tests or dpi_function nodes in the graph",
        )

    gb = _Builder()
    stage = BindingStage(graph=_empty_graph(gen), status=BUILT)
    scans: dict[Path, PyScan] = {}

    for test in sorted(cocotb_tests, key=lambda n: n["id"]):
        _bind_one_test(gb, stage, index, root, test, scans)

    _bind_dpi(
        gb,
        stage,
        index,
        root,
        verif_dir=verif_dir if verif_dir is not None else root / "verif",
        spec_dir=spec_dir if spec_dir is not None else root / "spec",
    )

    stage.graph["nodes"] = gb.node_list()
    stage.graph["links"] = gb.link_list()
    stage.modules = sum(
        1 for n in stage.graph["nodes"] if n.get("type") == PYTHON_MODULE_TYPE
    )
    log_event(
        logger,
        logging.DEBUG,
        "graph_bind.completed",
        tests=stage.tests,
        drives=stage.drives,
        inferred=stage.inferred,
        checks=stage.checks,
        dpi_implemented=stage.dpi_implemented,
    )
    return stage


def _suite_dir_of(test_id: str) -> str:
    """Suite directory encoded in a ``test:<suite dir>#<name>`` id."""
    body = test_id[len("test:") :] if test_id.startswith("test:") else test_id
    return body.split("#", 1)[0]


def _bind_one_test(
    gb: _Builder,
    stage: BindingStage,
    index: _Index,
    root: Path,
    test: dict,
    scans: dict[Path, PyScan],
) -> None:
    test_id = test["id"]
    suite_rel = _suite_dir_of(test_id)
    suite_dir = root / suite_rel
    tb_id = index.runs_on.get(test_id)
    toplevel = index.toplevel.get(tb_id) if tb_id else None
    ports = index.ports.get(toplevel) if toplevel else None

    bound = False
    for module_name in test.get("cocotb_modules") or []:
        path = resolve_module_file(module_name, [suite_dir])
        if path is None:
            missing_rel = f"{suite_rel}/{module_name.replace('.', '/')}.py"
            node_id, reused = _python_node(
                gb, index, missing_rel, module_name, exists=False, cocotb_module=True
            )
            stage.unresolved.append(
                {"test": test_id, "cocotb_module": module_name, "expected": missing_rel}
            )
            log_event(
                logger,
                logging.WARNING,
                "graph_bind.cocotb_module_not_found",
                test=test_id,
                module=module_name,
                expected=missing_rel,
            )
        else:
            node_id, reused = _python_node(
                gb, index, _rel(root, path), module_name, cocotb_module=True
            )
        if reused:
            stage.reused_ids += 1

        gb.add_link(test_id, node_id, BINDS_TO)
        bound = True
        if toplevel:
            # The other half of the two-hop path test -> module: the
            # Python module IS the testbench, so it binds to the DUT.
            gb.add_link(node_id, f"module:{toplevel}", BINDS_TO, toplevel=toplevel)

        if path is not None:
            _walk_module(
                gb,
                stage,
                index,
                root,
                test_id,
                node_id,
                path,
                suite_dir,
                toplevel,
                ports,
                scans,
            )
    if bound:
        stage.tests += 1


def _python_node(
    gb: _Builder,
    index: _Index,
    rel: str,
    label: str,
    *,
    exists: bool = True,
    cocotb_module: bool = False,
) -> tuple[str, bool]:
    """Node id for a Python file, reusing another tier's id when it has one.

    Returns ``(node id, reused)``. ``reused`` is the extractor hand-off
    having fired: some other tier already emitted a node for this file,
    so its id is adopted instead of a second ``py:`` node being invented
    for the same thing.
    """
    reused = rel in index.python_nodes
    node_id = index.python_nodes.get(rel, PY_NODE_PREFIX + rel)
    gb.add_node(
        node_id,
        PYTHON_MODULE_TYPE,
        label,
        file=rel,
        exists=None if exists else False,
        cocotb_module=True if cocotb_module else None,
    )
    return node_id, reused


def _scan(path: Path, scans: dict[Path, PyScan]) -> PyScan:
    scan = scans.get(path)
    if scan is None:
        scan = scan_python_source(path)
        scans[path] = scan
    return scan


def _walk_module(
    gb: _Builder,
    stage: BindingStage,
    index: _Index,
    root: Path,
    test_id: str,
    entry_node: str,
    entry_path: Path,
    suite_dir: Path,
    toplevel: str | None,
    ports: set[str] | None,
    scans: dict[Path, PyScan],
) -> None:
    """Walk the cocotb module and its local imports, emitting the edges.

    The walk is breadth-first over *local* imports only — a module that
    resolves to a file inside the project. ``import cocotb`` and
    ``import random`` resolve to nothing here and are dropped, which is
    the point: the graph is about this project, not its dependencies.

    A golden model is the exception to "local file wins": it lives under
    ``spec/`` and a cocotb suite reaches it through a runtime
    ``sys.path`` insert, which no static resolver should try to emulate.
    The config tier already emitted a ``golden_model`` node per
    ``spec/<block>/<model>.py``, so an import whose name matches one of
    those stems is bound to that node instead.
    """
    queue: list[tuple[Path, str, int]] = [(entry_path, entry_node, 0)]
    seen: set[Path] = {entry_path}

    while queue:
        path, node_id, depth = queue.pop(0)
        scan = _scan(path, scans)
        rel = _rel(root, path)
        via = None if path == entry_path else rel

        for name, line in sorted(scan.accesses.items()):
            _drive(gb, stage, toplevel, ports, node_id, name, line, rel, None)
            if via is not None:
                # The same fact, inherited by the cocotb module through
                # the helper. `via` is what says it is second-hand.
                _drive(gb, stage, toplevel, ports, entry_node, name, line, rel, via)

        if depth >= _MAX_IMPORT_DEPTH:
            continue
        for imported in scan.imports:
            target = resolve_module_file(imported, [suite_dir, path.parent])
            if target is None or not _inside(root, target):
                golden_id = index.golden.get(imported.split(".")[-1])
                if golden_id is not None and gb.add_link(
                    test_id, golden_id, CHECKS_AGAINST, via=via
                ):
                    stage.checks += 1
                continue
            target_id, _ = _python_node(gb, index, _rel(root, target), imported)
            gb.add_link(node_id, target_id, IMPORTS)
            if target not in seen:
                seen.add(target)
                queue.append((target, target_id, depth + 1))


def _bind_dpi(
    gb: _Builder,
    stage: BindingStage,
    index: _Index,
    root: Path,
    *,
    verif_dir: str | os.PathLike | None,
    spec_dir: str | os.PathLike | None,
) -> None:
    """``implemented_by`` edges: DPI C symbols -> the sources defining them.

    The design tier (rtl-buddy-sch 127) contributes one
    ``dpi_function`` node per ``import "DPI-C"`` / ``export "DPI-C"``
    item, keyed by C symbol. For every *imported* one — imports are the
    C-implements-it direction; an export is implemented on the SV side,
    so a C file naming it is a caller, not an implementation — the C
    symbol is matched against the C/C++/Python sources under ``verif/``
    and ``spec/``:

    Confidence follows the evidence, on the ladder :func:`_drive` uses —
    ``EXTRACTED`` is reserved for a fact the scan actually established,
    which for an implementation means a *definition site*, not a mention:

    1. an exact-case definition (``<declarator> sym(...) {``, or
       ``def sym(`` in Python) -> EXTRACTED;
    2. an exact-case whole-word *mention* -> INFERRED, ``resolved:
       false`` — a header's declaration and a caller both look like this,
       and neither implements anything;
    3. a case-insensitive definition -> INFERRED (name similarity);
    4. a case-insensitive mention -> INFERRED, ``resolved: false``.

    The best rung present wins outright: the realistic project of
    ``alu_ref.h`` declaring, ``alu_ref.c`` defining and ``tb_driver.c``
    calling gets **one** edge — the definition — rather than three
    equally confident ones an agent cannot choose between. Mentions
    survive only when nothing defines the symbol at all.

    Only ``direction: "import"`` binds. A node that omits the field is a
    no-op rather than an assumed import: the extraction half is
    unreleased, so a looser future extractor would otherwise silently
    bind exports — the caller-vs-implementation confusion the export skip
    exists to prevent — and this stage's whole design is to under-claim on
    vocabulary it does not recognise.

    The edge target reuses a node another tier already gave the file —
    a ``golden_model`` first (a DPI reference model under ``spec/`` is
    exactly the golden-model loop this closes), then the extractor's
    Python node — and otherwise synthesizes ``py:``/``src:`` nodes the
    same way the cocotb pass does. A symbol nothing defines is recorded
    in ``unresolved``; a graph with no ``dpi_function`` nodes at all
    (any extractor predating them) makes this a silent no-op.
    """
    imports = [
        node
        for node in index.dpi
        if node.get("direction") == "import" and node.get("id")
    ]
    skipped = [
        node
        for node in index.dpi
        if node.get("id") and node.get("direction") not in ("import", "export")
    ]
    if skipped:
        log_event(
            logger,
            logging.DEBUG,
            "graph_bind.dpi_direction_unknown",
            count=len(skipped),
            example=str(skipped[0]["id"]),
            direction=str(skipped[0].get("direction")),
        )
    if not imports:
        return
    sources = [
        Path(p)
        for p in collect_sources(verif_dir, spec_dir)
        # ``resolve_module_file`` never leaves the project; the DPI scan
        # must not either.
        if _inside(root, Path(p))
    ]
    texts: list[tuple[Path, str]] = []
    for path in sources:
        body = _read_text(path)
        if body is not None:
            texts.append((path, body))

    for node in sorted(imports, key=lambda n: str(n["id"])):
        symbol = node.get("c_symbol") or node.get("label")
        if not symbol and str(node["id"]).startswith("dpi:"):
            symbol = str(node["id"])[len("dpi:") :]
        if not symbol:
            continue
        stage.dpi_functions += 1
        exact = re.compile(rf"\b{re.escape(symbol)}\b")
        similar = re.compile(rf"\b{re.escape(symbol)}\b", re.IGNORECASE)
        # (rung, path, offset, confidence, resolved). The rung ladder is
        # what keeps `EXTRACTED` meaning *this file defines it*: a bare
        # mention is a header's declaration or a caller, which is exactly
        # the second-hand evidence `resolved: false` exists to mark.
        matches: list[tuple[int, Path, int, str, bool | None]] = []
        for path, body in texts:
            offset = _definition_offset(path, body, symbol, ignore_case=False)
            if offset is not None:
                matches.append((1, path, offset, EXTRACTED, None))
                continue
            hit = exact.search(body)
            if hit is not None:
                matches.append((2, path, hit.start(), INFERRED, False))
                continue
            offset = _definition_offset(path, body, symbol, ignore_case=True)
            if offset is not None:
                matches.append((3, path, offset, INFERRED, None))
                continue
            hit = similar.search(body)
            if hit is not None:
                matches.append((4, path, hit.start(), INFERRED, False))
        if matches:
            # Best rung wins outright, so a project with `alu_ref.h`
            # declaring, `alu_ref.c` defining and `tb_driver.c` calling
            # gets one edge — the definition — instead of three equally
            # confident ones. Mentions only survive when nothing defines
            # the symbol at all, and say so with `resolved: false`.
            best = min(rung for rung, *_ in matches)
            matches = [m for m in matches if m[0] == best]
        if not matches:
            stage.unresolved.append({"dpi_symbol": symbol, "node": node["id"]})
            log_event(
                logger,
                logging.WARNING,
                "graph_bind.dpi_symbol_not_found",
                symbol=symbol,
                node=node["id"],
            )
            continue
        for _, path, offset, confidence, resolved in matches:
            rel = _rel(root, path)
            target = _source_file_node(gb, index, rel)
            body = next(text for candidate, text in texts if candidate == path)
            if gb.add_link(
                str(node["id"]),
                target,
                IMPLEMENTED_BY,
                confidence,
                symbol=symbol,
                file=rel,
                line=body.count("\n", 0, offset) + 1,
                resolved=None if resolved is None else False,
            ):
                stage.dpi_implemented += 1


#: Words that, immediately before a `symbol(` occurrence, prove the
#: occurrence is a *call* and not a declarator — `return add_ref(a, b) {`
#: cannot happen, but `if (x) add_ref(a) {` shaped text can be produced by
#: enough macro soup that the cheap guard is worth having.
_CALL_PREFIX_WORDS = frozenset(
    {
        "if",
        "while",
        "for",
        "switch",
        "return",
        "else",
        "do",
        "case",
        "sizeof",
        "and",
        "or",
        "not",
    }
)

#: A declarator's prefix on the definition line — a return type, possibly
#: with qualifiers, pointers, namespaces or template arguments. Anything
#: with an `=`, a `(` or a `,` in it is an expression, not a declarator.
_DECLARATOR_PREFIX = re.compile(r"[A-Za-z_][\w\s*&:<>\[\]]*")


def _c_definition_offset(body: str, symbol: str, *, ignore_case: bool) -> int | None:
    """Offset of a C-family *definition* of ``symbol``, or None.

    A definition is `<declarator> symbol(<params>) {` — the brace is what
    separates it from the declaration in a header and from the call site
    in a driver, which are the two things a bare whole-word scan cannot
    tell apart from an implementation.
    """
    flags = re.IGNORECASE if ignore_case else 0
    for match in re.finditer(rf"\b{re.escape(symbol)}\b", body, flags):
        rest = body[match.end() :]
        stripped = rest.lstrip()
        if not stripped.startswith("("):
            continue
        # Walk the parameter list; a `;` or `{` inside it means this was
        # never a signature.
        depth = 0
        end: int | None = None
        for idx in range(match.end() + (len(rest) - len(stripped)), len(body)):
            char = body[idx]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end = idx + 1
                    break
            elif char in ";{":
                break
        if end is None:
            continue
        tail = re.sub(
            r"^(?:const|noexcept|override|final)\b\s*", "", body[end:].lstrip()
        )
        if not tail.startswith("{"):
            continue
        line_start = body.rfind("\n", 0, match.start()) + 1
        prefix = body[line_start : match.start()].strip()
        if prefix:
            if not _DECLARATOR_PREFIX.fullmatch(prefix):
                continue
            words = re.findall(r"[A-Za-z_]\w*", prefix)
            if words and words[-1] in _CALL_PREFIX_WORDS:
                continue
        return match.start()
    return None


def _py_definition_offset(body: str, symbol: str, *, ignore_case: bool) -> int | None:
    """Offset of a ``def symbol(`` / ``async def symbol(`` line, or None."""
    flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
    match = re.search(
        rf"^[ \t]*(?:async[ \t]+)?def[ \t]+{re.escape(symbol)}[ \t]*\(", body, flags
    )
    return None if match is None else match.start()


def _definition_offset(path: Path, body: str, symbol: str, *, ignore_case: bool):
    finder = (
        _py_definition_offset if path.suffix.lower() == ".py" else _c_definition_offset
    )
    return finder(body, symbol, ignore_case=ignore_case)


def _source_file_node(gb: _Builder, index: _Index, rel: str) -> str:
    """Node id for a source file an ``implemented_by`` edge lands on.

    Reuse order: the config tier's ``golden_model`` node (pointing the
    DPI function straight at the model closes the golden-model loop in
    one hop), then any node another tier already gave the ``.py`` file,
    then a synthesized ``py:`` / ``src:`` node.
    """
    existing = index.golden_files.get(rel) or index.python_nodes.get(rel)
    if existing is not None:
        return existing
    if rel.endswith(".py"):
        node_id, _ = _python_node(gb, index, rel, Path(rel).stem)
        return node_id
    return gb.add_node(
        SRC_NODE_PREFIX + rel, SOURCE_FILE_TYPE, Path(rel).name, file=rel
    )


def _inside(root: Path, path: Path) -> bool:
    try:
        Path(os.path.realpath(str(path))).relative_to(root)
    except ValueError:
        return False
    return True


def _drive(
    gb: _Builder,
    stage: BindingStage,
    toplevel: str | None,
    ports: set[str] | None,
    source: str,
    name: str,
    line: int,
    file_rel: str,
    via: str | None,
) -> None:
    """Emit one ``drives`` edge for a ``dut.<name>`` access.

    Confidence is decided by the port table and nothing else:

    * ``name`` is a port of the toplevel -> EXTRACTED;
    * it differs only in case -> INFERRED, pointing at the real port;
    * no port matches, or no design tier was exported so no port is
      known -> INFERRED, pointing at ``port:<top>.<name>``, which may
      well dangle. ``resolved: false`` marks the ones known not to be
      ports so a consumer can filter them out.
    """
    if not toplevel:
        return
    resolved = True
    confidence = INFERRED
    port_name = name
    if ports is None:
        pass  # design tier absent: nothing to check the name against
    elif name in ports:
        confidence = EXTRACTED
    else:
        lowered = {p.lower(): p for p in ports}
        match = lowered.get(name.lower())
        if match is not None:
            port_name = match
        else:
            resolved = False

    added = gb.add_link(
        source,
        f"port:{toplevel}.{port_name}",
        DRIVES,
        confidence,
        signal=name,
        file=file_rel,
        line=line,
        via=via,
        resolved=None if resolved else False,
    )
    if not added:
        return
    stage.drives += 1
    if confidence == EXTRACTED:
        stage.extracted += 1
    else:
        stage.inferred += 1
    if not resolved:
        stage.unresolved.append(
            {
                "access": f"dut.{name}",
                "toplevel": toplevel,
                "file": file_rel,
                "line": line,
            }
        )
