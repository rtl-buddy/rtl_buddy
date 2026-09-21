# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The optional binding-tier extractor for ``rb graph build`` (#377/#391).

The extractor (`rtl-buddy-graph-extract`, a satellite package) contributes
the **binding tier**: the Python-level structure of cocotb tests,
preproc/postproc hooks and golden models, plus the section structure of
the spec markdown. It is an *optional* dependency — with it absent
``rb graph build`` still writes a design + config graph and says so in
the envelope — so everything here is written to degrade instead of raise:

* the tool is discovered through :mod:`rtl_buddy.tool_manifest`, the same
  gate ``rb tool-check`` reports on;
* a non-zero exit, a missing output file, or output that isn't node-link
  JSON marks the tier ``failed`` with a reason and leaves the other tiers
  intact.

The argv shapes below are the producer/consumer contract, owned by
rtl-buddy and restated in the extractor repo's
``docs/extract-contract.md``; both suites assert it, so a drift on
either side fails one of them. Change it in lockstep or not at all.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from ..logging_utils import log_event
from ..process_utils import run_managed_process
from ..tool_manifest import check_tool, get_manifest

logger = logging.getLogger(__name__)

#: Manifest name of the bundled extractor package (rtl_buddy#391) and
#: the binary it installs.
GRAPH_EXTRACT_TOOL = "rtl-buddy-graph-extract"
GRAPH_EXTRACT_BINARY = "rb-graph-extract"

#: Subcommand that runs the deterministic extraction pass.
EXTRACT_VERB = "extract"

#: Subcommand that unions node-link graphs. Used only as a cross-check —
#: ``rtl_buddy.graph.merge`` owns the merge that actually ships.
MERGE_VERB = "merge-graphs"

#: Emitted (and expected) envelope format.
GRAPH_FORMAT = "node-link"

#: Seconds before an extractor subprocess is abandoned. The pass is
#: optional, so a hung tool must not hang the build.
DEFAULT_TIMEOUT = 900

#: Files handed to the deterministic pass, by tree.
VERIF_SUFFIXES = (".py",)
SPEC_SUFFIXES = (".md",)

#: Directories never descended into when collecting extractor inputs.
_SKIP_DIRS = frozenset(
    {".git", "__pycache__", "artefacts", "obj_dir", "node_modules", "venv", ".venv"}
)


@dataclass
class ExtractResult:
    """Outcome of one extractor invocation.

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


@dataclass(frozen=True)
class ExtractorChoice:
    """The binding-tier extractor `rb graph build` decided to run.

    Attributes:
      executable (str): binary handed to :func:`run_extract`.
      version (str): probed version, or ``"unknown"`` for a tool that
        is present but would not report one — either way the string
        lands in the build fingerprint, so an upgrade invalidates the
        cached build.
    """

    executable: str
    version: str


def resolve_extractor(root_cfg=None) -> ExtractorChoice | None:
    """The bundled extractor when installed, else None (tier skipped)."""
    spec = next(
        (s for s in get_manifest(root_cfg) if s.name == GRAPH_EXTRACT_TOOL), None
    )
    if spec is None:  # pragma: no cover - manifest always carries it
        return None
    status = check_tool(spec)
    if status.status == "missing":
        return None
    return ExtractorChoice(GRAPH_EXTRACT_BINARY, status.version or "unknown")


def collect_inputs(
    verif_dir: str | os.PathLike | None, spec_dir: str | os.PathLike | None
) -> list[str]:
    """Absolute paths of the verif Python and spec markdown the extractor reads.

    Deliberately narrow: the deterministic pass is about Python
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
    executable: str, inputs: list[str], output: str | os.PathLike
) -> list[str]:
    """argv for the extractor's deterministic extraction pass."""
    return [
        executable,
        EXTRACT_VERB,
        "--format",
        GRAPH_FORMAT,
        "--output",
        str(output),
        *[str(p) for p in inputs],
    ]


def build_merge_cmd(
    executable: str, inputs: list[str], output: str | os.PathLike
) -> list[str]:
    """argv for the extractor's ``merge-graphs`` verb (cross-check only)."""
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
    executable: str = GRAPH_EXTRACT_BINARY,
    log_path: str | os.PathLike | None = None,
    cwd: str | None = None,
) -> ExtractResult:
    """Run the extractor's deterministic pass over ``inputs``.

    Never raises: a failure is reported through
    :class:`ExtractResult` so the caller can mark the binding tier
    failed and still write the merged design + config graph.
    """
    if not inputs:
        return ExtractResult(ok=False, detail="no verif Python or spec markdown found")
    cmd = build_extract_cmd(executable, inputs, output)
    log_event(
        logger,
        logging.DEBUG,
        "graph_build.extract_start",
        verb=EXTRACT_VERB,
        inputs=len(inputs),
    )
    rc, tail = _run(cmd, log_path, cwd)
    if rc != 0:
        return ExtractResult(ok=False, detail=f"exit {rc}: {tail}".strip(), cmd=cmd)
    graph, err = _load_graph(output)
    if graph is None:
        return ExtractResult(ok=False, detail=err, cmd=cmd)
    return ExtractResult(ok=True, graph=graph, cmd=cmd)


def run_merge_cross_check(
    tier_files: list[str],
    output: str | os.PathLike,
    *,
    internal: dict,
    executable: str = GRAPH_EXTRACT_BINARY,
    log_path: str | os.PathLike | None = None,
    cwd: str | None = None,
) -> dict:
    """Compare the extractor's ``merge-graphs`` against the internal union.

    The internal merge is the one that ships — this only reports whether
    the extractor's union agrees, so a divergence in either
    implementation surfaces instead of hiding. The returned dict lands
    in ``graph-meta.json`` under ``merge.extract_cross_check``.
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
    only_extract = sorted(x for x in theirs - ours if x)
    result = {
        "status": "ok" if not only_internal and not only_extract else "mismatch",
        "internal_nodes": len(ours),
        "extract_nodes": len(theirs),
        "internal_links": len(internal.get("links") or []),
        "extract_links": len(graph.get("links") or []),
    }
    # Bounded: a wholesale disagreement should not blow up the sidecar.
    if only_internal:
        result["only_internal"] = only_internal[:20]
    if only_extract:
        result["only_extract"] = only_extract[:20]
    if result["status"] == "mismatch":
        log_event(
            logger,
            logging.WARNING,
            "graph_build.extract_merge_mismatch",
            only_internal=len(only_internal),
            only_extract=len(only_extract),
        )
    return result
