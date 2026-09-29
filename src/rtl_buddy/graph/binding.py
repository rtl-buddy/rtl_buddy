# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Post-merge binding stage: ties cocotb tests and DPI symbols to the design hierarchy.

It runs after the tiers are merged because it needs the config tier's `test`/`testbench`
nodes and the design tier's `port:<top>.<name>` nodes together. It works without the
extractor: it reuses the extractor's Python nodes when present (matched on the
repo-relative `file`) and otherwise synthesizes `py:<repo-rel path>` nodes.
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

# `tier` stamped on everything this stage emits; the extractor contributes to the same
# tier.
BINDING_TIER = "binding"

# Synthesized Python module node; used only when no other tier's node claims the file.
PYTHON_MODULE_TYPE = "python_module"
PY_NODE_PREFIX = "py:"

# Synthesized node for a non-Python source file a DPI symbol resolves to; same
# claim-by-`file` rule.
SOURCE_FILE_TYPE = "source_file"
SRC_NODE_PREFIX = "src:"

# Design-tier node type for one `import "DPI-C"` / `export "DPI-C"` item.
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

# Default DUT handle name; a `@cocotb.test()` function's first parameter is added per
# file.
DEFAULT_HANDLE = "dut"

# Handle API attributes that are not signals. Names starting with `_` are dropped too.
_HANDLE_API_ATTRS = frozenset(
    {"value", "setimmediatevalue", "get", "keys", "items", "log", "range"}
)

# Bounds import cycles and long helper chains.
_MAX_IMPORT_DEPTH = 8

# Largest Python file read during the scan, in bytes.
_MAX_SCAN_BYTES = 1 << 20

# Directories skipped when collecting stage inputs.
_SKIP_DIRS = frozenset(
    {".git", "__pycache__", "artefacts", "obj_dir", "node_modules", "venv", ".venv"}
)

_DUT_ACCESS_RE = re.compile(r"\bdut\.([A-Za-z_]\w*)")
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.MULTILINE)

# Non-Python suffixes read by the DPI symbol scan.
_C_SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp")


# ---------------------------------------------------------------------------
# Source scanning
# ---------------------------------------------------------------------------


@dataclass
class PyScan:
    """What one Python file contributes to the binding stage.

    `accesses` maps `dut.<name>` to the first line it appears on. `imports` lists
    imported module names in source order. `parsed` is False when the regex fallback was
    used.
    """

    path: Path
    accesses: dict[str, int] = dc_field(default_factory=dict)
    imports: list[str] = dc_field(default_factory=list)
    parsed: bool = True


def _decorator_name(node: ast.expr) -> str:
    """Return the dotted name of a decorator expression (`cocotb.test()` -> `cocotb.test`)."""
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
    """Return the DUT-handle names in a module.

    `dut`, plus the first parameter of every `@cocotb.test()` function.
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

    Uses `ast`, so `dut.a` is told apart from the string `"dut.a"` and `self.dut.a`. A
    file that does not parse falls back to a regex sweep.
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
            # Only absolute imports resolve; cocotb imports the suite's modules flat.
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
    """Return absolute paths of every Python and C/C++ source the stage may read.

    A superset of what one build parses, because it is the fingerprint input list.
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
    """Lookups over the merged graph."""

    nodes: dict[str, dict] = dc_field(default_factory=dict)
    # Module name -> port names, from `port:` nodes.
    ports: dict[str, set[str]] = dc_field(default_factory=dict)
    # Testbench node id -> toplevel.
    toplevel: dict[str, str] = dc_field(default_factory=dict)
    # Test node id -> testbench node id, from `runs_on` links.
    runs_on: dict[str, str] = dc_field(default_factory=dict)
    # Golden model stem -> node id.
    golden: dict[str, str] = dc_field(default_factory=dict)
    # Repo-relative `.py` path -> existing node id (the extractor's).
    python_nodes: dict[str, str] = dc_field(default_factory=dict)
    # Repo-relative path -> `golden_model` node id; preferred `implemented_by` target.
    golden_files: dict[str, str] = dc_field(default_factory=dict)
    # `dpi_function` nodes from the design tier, in id order.
    dpi: list[dict] = dc_field(default_factory=list)


def _existing_python_nodes(nodes: list[dict]) -> dict[str, str]:
    """Return node ids already claiming a `.py` file, keyed by that file.

    Other tiers name Python nodes freely, so the repo-relative `file` is the shared key.
    `golden_model` and `spec_doc` nodes describe a file rather than being the module,
    and are excluded.
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
        """Add a link unless `(source, target, type)` already exists; return True when new.

        This dedups more coarsely than `merge_graphs`, and the walk is breadth-first, so
        the first sighting is the most direct evidence.
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
      graph: Node-link payload holding only this stage's nodes and links.
      status: `built` or `skipped`.
      detail: Why, when `skipped`.
      tests: cocotb tests that got a `binds_to` edge.
      modules: Python module nodes touched.
      reused_ids: Modules that reused an id another tier gave the file.
      drives: `drives` edges emitted.
      extracted: `drives` edges that matched a port exactly.
      inferred: The remaining `drives` edges.
      checks: `checks_against` edges emitted.
      dpi_functions: `dpi_function` imports searched for an implementation.
      dpi_implemented: `implemented_by` edges emitted.
    unresolved: Missing cocotb module files, `dut.<name>` accesses that matched no port,
    and DPI symbols no source defines.
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
            # Bounded so a suite with a typo'd handle cannot bloat the sidecar.
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
    """Return the file backing the import `name`, or None.

    Searches the way cocotb sees it: `search_dirs` in order, for `x.py` then
    `x/__init__.py`.
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
    """Bind cocotb Python and DPI C symbols to the hierarchy in `merged`.

    Edges emitted:

    - `binds_to`: test -> Python module, and Python module -> DUT `module:` node. Both
      come from `tests.yaml` (`cocotb: {module: M}`, `toplevel:`) and are EXTRACTED.
    - `imports`: Python module -> Python module, from an `ast` parse of the import
      statements.
    - `drives`: Python module -> `port:`, from a `dut.<name>` attribute scan. EXTRACTED
      when `<name>` is a port of the toplevel, INFERRED otherwise. A helper module's
      edge is repeated on the importing module with `via` naming the helper file, so
      first-hand and inherited evidence stay distinguishable.
    - `checks_against`: test -> `golden_model`, when the cocotb module imports one
      directly or through a helper.
    - `implemented_by`: `dpi_function` -> the source file defining its C symbol.
      EXTRACTED for a definition site, INFERRED with `resolved: false` for a mention.
      Graphs without `dpi_function` nodes skip this pass.

    Args:
    merged: The merged graph after the design and config tiers are unioned. Not
    modified; the contribution comes back in `BindingStage.graph`.
    project_root: Directory holding `root_config.yaml`; node-id paths are relative to
    it.
      generator: `graph.generator` block for the emitted graph.
    verif_dir, spec_dir: Roots for the DPI source scan. Default to
    `<project_root>/verif` and `<project_root>/spec`.

    Never raises: unparseable helpers, missing cocotb modules, accesses matching no port
    and DPI symbols nothing defines are recorded in `unresolved` and the pass continues.
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
    """Return the suite directory encoded in a `test:<suite dir>#<name>` id."""
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
            # The Python module is the testbench, so it binds to the DUT.
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
    """Return `(node id, reused)` for a Python file.

    Adopts another tier's node id for the file when it has one, so no second `py:` node
    is invented.
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
    """Walk the cocotb module and its local imports breadth-first, emitting the edges.

    Only imports that resolve to a file inside the project are followed, so `import
    cocotb` is dropped. A golden model is the exception: it lives under `spec/` and is
    reached through a runtime `sys.path` insert, so an import whose name matches a
    config-tier `golden_model` stem binds to that node instead.
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
                # The same fact, inherited by the cocotb module; `via` marks it
                # second-hand.
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
    """Add `implemented_by` edges from DPI import symbols to the sources defining them.

    Only `direction: "import"` nodes bind; an export is implemented on the SV side, so a
    C file naming it is a caller. A node with no direction is skipped. Each symbol is
    matched against the C/C++/Python sources under `verif/` and `spec/`, with confidence
    by rung:

    1. an exact-case definition (`<declarator> sym(...) {`, or `def sym(`) -> EXTRACTED;
    2. an exact-case whole-word mention -> INFERRED, `resolved: false`;
    3. a case-insensitive definition -> INFERRED;
    4. a case-insensitive mention -> INFERRED, `resolved: false`.

    The best rung present wins, so a header declaring, a `.c` defining and a driver
    calling the symbol yield one edge, to the definition. The target is the file's
    `golden_model` node, else the extractor's Python node, else a synthesized
    `py:`/`src:` node. A symbol nothing defines goes to `unresolved`.
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
        # The DPI scan, like `resolve_module_file`, must not leave the project.
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
        # (rung, path, offset, confidence, resolved). The ladder keeps EXTRACTED meaning
        # "this file defines it".
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
            # The best rung wins outright; mentions survive only when nothing defines
            # the symbol.
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


# A word before `symbol(` that proves the occurrence is a call, not a declarator.
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

# A declarator prefix: a return type with optional qualifiers, pointers, namespaces or
# template arguments. An `=`, `(` or `,` makes it an expression.
_DECLARATOR_PREFIX = re.compile(r"[A-Za-z_][\w\s*&:<>\[\]]*")


def _c_definition_offset(body: str, symbol: str, *, ignore_case: bool) -> int | None:
    """Return the offset of a C-family definition of `symbol`, or None.

    A definition is `<declarator> symbol(<params>) {`; the brace separates it from a
    header declaration and from a call site.
    """
    flags = re.IGNORECASE if ignore_case else 0
    for match in re.finditer(rf"\b{re.escape(symbol)}\b", body, flags):
        rest = body[match.end() :]
        stripped = rest.lstrip()
        if not stripped.startswith("("):
            continue
        # A `;` or `{` inside the parameter list means this is not a signature.
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
    """Return the offset of a `def symbol(` / `async def symbol(` line, or None."""
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
    """Return the node id for a source file an `implemented_by` edge lands on.

    Reuse order: the `golden_model` node, then any node another tier gave the `.py`
    file, then a synthesized `py:`/`src:` node.
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
    """Emit one `drives` edge for a `dut.<name>` access.

    Confidence comes from the port table:

    - `name` is a port of the toplevel -> EXTRACTED.
    - Differs only in case -> INFERRED, pointing at the real port.
    - No port matches, or no design tier is present -> INFERRED, pointing at
      `port:<top>.<name>`, which may dangle. `resolved: false` marks names known not to
      be ports.
    """
    if not toplevel:
        return
    resolved = True
    confidence = INFERRED
    port_name = name
    if ports is None:
        pass  # design tier absent: no port names to check
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
