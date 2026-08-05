# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Optional Graphify integration for ``rb graph build`` (#377).

Graphify contributes the **binding tier**: the Python-level structure of
cocotb tests, preproc/postproc hooks and golden models, plus whatever it
reads out of the spec markdown. It is an *optional* dependency — with it
absent ``rb graph build`` still writes a design + config graph and says
so in the envelope — so everything here is written to degrade instead of
raise:

* the tool is discovered through :mod:`rtl_buddy.tool_manifest`, the same
  gate ``rb tool-check`` reports on;
* the semantic (LLM) pass is **never** run unless explicitly asked for,
  because it ships project source to a configured model;
* a non-zero exit, a missing output file, or output that isn't node-link
  JSON marks the tier ``failed`` with a reason and leaves the other tiers
  intact.

The argv shapes below are the contract this module assumes of the
Graphify CLI. They are kept as module constants (rather than inlined) so
reconciling them with the real tool is a one-place edit, and so tests can
assert on them without running Graphify.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from ..logging_utils import log_event
from ..process_utils import run_managed_process
from ..tool_manifest import ToolStatus, check_tool, get_manifest

logger = logging.getLogger(__name__)

#: Manifest name (and default binary name) of the Graphify CLI.
GRAPHIFY_TOOL = "graphify"

#: Manifest name of the bundled clean-room implementation of the same
#: CLI contract (rtl_buddy#391), and the binary it installs. Kept as a
#: separate manifest entry rather than a second binary on the graphify
#: spec so `rb tool-check` and the build fingerprint name the tool that
#: actually ran.
GRAPH_EXTRACT_TOOL = "rtl-buddy-graph-extract"
GRAPH_EXTRACT_BINARY = "rb-graph-extract"

#: Subcommand that runs Graphify's deterministic extraction pass.
EXTRACT_VERB = "extract"

#: Subcommand that unions node-link graphs. Used only as a cross-check —
#: ``rtl_buddy.graph.merge`` owns the merge that actually ships.
MERGE_VERB = "merge-graphs"

#: Emitted (and expected) envelope format.
GRAPH_FORMAT = "node-link"

#: Flag that opts into Graphify's LLM-backed semantic pass. Off unless
#: the user passes ``--graphify-llm``; it sends file content to whatever
#: model Graphify is configured with.
LLM_FLAG = "--llm"

#: Seconds before a Graphify subprocess is abandoned. The pass is
#: optional, so a hung tool must not hang the build.
DEFAULT_TIMEOUT = 900

#: Files handed to the deterministic pass, by tree.
VERIF_SUFFIXES = (".py",)
SPEC_SUFFIXES = (".md",)

#: Directories never descended into when collecting Graphify inputs.
_SKIP_DIRS = frozenset(
    {".git", "__pycache__", "artefacts", "obj_dir", "node_modules", "venv", ".venv"}
)


@dataclass
class GraphifyResult:
    """Outcome of one Graphify invocation.

    Attributes:
      ok (bool): True when the graph was produced and parsed.
      graph (dict | None): Parsed node-link payload when ``ok``.
      detail (str | None): Human-readable reason when not ``ok``.
      cmd (list[str]): argv actually executed (empty when nothing ran).
    """

    ok: bool
    graph: dict | None = None
    detail: str | None = None
    cmd: list[str] = dc_field(default_factory=list)


def graphify_status(root_cfg=None) -> ToolStatus | None:
    """``ToolStatus`` for Graphify, or None if it isn't in the manifest."""
    spec = next((s for s in get_manifest(root_cfg) if s.name == GRAPHIFY_TOOL), None)
    if spec is None:  # pragma: no cover - manifest always carries it
        return None
    return check_tool(spec)


@dataclass(frozen=True)
class ExtractorChoice:
    """The binding-tier extractor `rb graph build` decided to run.

    Attributes:
      tool (str): manifest name — also the fingerprint key, so swapping
        implementations invalidates the cached build.
      executable (str): binary handed to :func:`run_extract`.
      version (str): probed version, or ``"unknown"`` for a tool that
        is present but would not report one.
    """

    tool: str
    executable: str
    version: str


def resolve_extractor(root_cfg=None) -> ExtractorChoice | None:
    """First installed of Graphify, then the bundled clean-room tool.

    The original tool wins when both are present: the bundled
    implementation exists because Graphify is not installable, so
    someone who went out of their way to install the real thing has
    stated a preference. None means the binding tier is skipped.
    """
    by_name = {s.name: s for s in get_manifest(root_cfg)}
    for tool, executable in (
        (GRAPHIFY_TOOL, GRAPHIFY_TOOL),
        (GRAPH_EXTRACT_TOOL, GRAPH_EXTRACT_BINARY),
    ):
        spec = by_name.get(tool)
        if spec is None:  # pragma: no cover - manifest always carries both
            continue
        status = check_tool(spec)
        if status.status != "missing":
            return ExtractorChoice(tool, executable, status.version or "unknown")
    return None


def collect_inputs(
    verif_dir: str | os.PathLike | None, spec_dir: str | os.PathLike | None
) -> list[str]:
    """Absolute paths of the verif Python and spec markdown Graphify reads.

    Deliberately narrow: Graphify's deterministic pass is about Python
    structure and prose, and the RTL is already covered by the design
    tier. Sorted so the resulting hash list is stable.
    """
    found: list[str] = []
    for root, suffixes in ((verif_dir, VERIF_SUFFIXES), (spec_dir, SPEC_SUFFIXES)):
        if root is None or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for name in sorted(filenames):
                if name.endswith(suffixes):
                    found.append(os.path.abspath(os.path.join(dirpath, name)))
    return sorted(set(found))


def build_extract_cmd(
    executable: str,
    inputs: list[str],
    output: str | os.PathLike,
    *,
    llm: bool = False,
) -> list[str]:
    """argv for Graphify's deterministic extraction pass."""
    cmd = [
        executable,
        EXTRACT_VERB,
        "--format",
        GRAPH_FORMAT,
        "--output",
        str(output),
    ]
    if llm:
        cmd.append(LLM_FLAG)
    return cmd + [str(p) for p in inputs]


def build_merge_cmd(
    executable: str, inputs: list[str], output: str | os.PathLike
) -> list[str]:
    """argv for ``graphify merge-graphs`` (cross-check only)."""
    return [
        executable,
        MERGE_VERB,
        "--format",
        GRAPH_FORMAT,
        "--output",
        str(output),
        *[str(p) for p in inputs],
    ]


def _run(
    cmd: list[str], log_path: str | os.PathLike | None, cwd: str | None
) -> tuple[int, str]:
    """Run ``cmd``, tee stderr into ``log_path``, return (rc, stderr tail).

    Goes through :func:`~rtl_buddy.process_utils.run_managed_process`
    rather than plain ``subprocess.run``: an extraction pass over a large
    verif tree is a long-running tool invocation, and the optional tier
    must not be able to strand a child process or hang the build.
    """
    try:
        proc = run_managed_process(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=DEFAULT_TIMEOUT,
            timeout_returncode=124,
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return 127, f"{cmd[0]}: not found"
    if proc.timed_out:
        return 124, f"{cmd[0]}: timed out after {DEFAULT_TIMEOUT}s"
    if log_path is not None:
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as handle:
                handle.write("$ " + " ".join(cmd) + "\n")
                handle.write(proc.stdout or "")
                handle.write(proc.stderr or "")
        except OSError:  # pragma: no cover - log is best effort
            pass
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return proc.returncode, tail[-1] if tail else ""


def _load_graph(path: str | os.PathLike) -> tuple[dict | None, str | None]:
    try:
        data = json.loads(Path(path).read_text())
    except OSError:
        return None, f"no output written at {path}"
    except json.JSONDecodeError as exc:
        return None, f"output is not JSON: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        return None, "output is not node-link JSON (no 'nodes' list)"
    return data, None


def run_extract(
    inputs: list[str],
    output: str | os.PathLike,
    *,
    executable: str = GRAPHIFY_TOOL,
    llm: bool = False,
    log_path: str | os.PathLike | None = None,
    cwd: str | None = None,
) -> GraphifyResult:
    """Run Graphify's deterministic pass over ``inputs``.

    Never raises: a failure is reported through
    :class:`GraphifyResult` so the caller can mark the binding tier
    failed and still write the merged design + config graph.
    """
    if not inputs:
        return GraphifyResult(ok=False, detail="no verif Python or spec markdown found")
    cmd = build_extract_cmd(executable, inputs, output, llm=llm)
    log_event(
        logger,
        logging.DEBUG,
        "graph_build.graphify_start",
        verb=EXTRACT_VERB,
        inputs=len(inputs),
        llm=llm,
    )
    rc, tail = _run(cmd, log_path, cwd)
    if rc != 0:
        return GraphifyResult(ok=False, detail=f"exit {rc}: {tail}".strip(), cmd=cmd)
    graph, err = _load_graph(output)
    if graph is None:
        return GraphifyResult(ok=False, detail=err, cmd=cmd)
    return GraphifyResult(ok=True, graph=graph, cmd=cmd)


def run_merge_cross_check(
    tier_files: list[str],
    output: str | os.PathLike,
    *,
    internal: dict,
    executable: str = GRAPHIFY_TOOL,
    log_path: str | os.PathLike | None = None,
    cwd: str | None = None,
) -> dict:
    """Compare ``graphify merge-graphs`` against the internal union.

    The internal merge is the one that ships — this only reports whether
    Graphify agrees, so a divergence in either implementation surfaces
    instead of hiding. The returned dict lands in ``graph-meta.json``
    under ``merge.graphify_cross_check``.
    """
    if len(tier_files) < 2:
        return {"status": "skipped", "detail": "fewer than two tier files"}
    cmd = build_merge_cmd(executable, tier_files, output)
    rc, tail = _run(cmd, log_path, cwd)
    if rc != 0:
        return {"status": "failed", "detail": f"exit {rc}: {tail}".strip()}
    graph, err = _load_graph(output)
    if graph is None:
        return {"status": "failed", "detail": err}

    ours = {n.get("id") for n in internal.get("nodes") or []}
    theirs = {n.get("id") for n in graph.get("nodes") or []}
    only_internal = sorted(x for x in ours - theirs if x)
    only_graphify = sorted(x for x in theirs - ours if x)
    result = {
        "status": "ok" if not only_internal and not only_graphify else "mismatch",
        "internal_nodes": len(ours),
        "graphify_nodes": len(theirs),
        "internal_links": len(internal.get("links") or []),
        "graphify_links": len(graph.get("links") or []),
    }
    # Bounded: a wholesale disagreement should not blow up the sidecar.
    if only_internal:
        result["only_internal"] = only_internal[:20]
    if only_graphify:
        result["only_graphify"] = only_graphify[:20]
    if result["status"] == "mismatch":
        log_event(
            logger,
            logging.WARNING,
            "graph_build.graphify_merge_mismatch",
            only_internal=len(only_internal),
            only_graphify=len(only_graphify),
        )
    return result
