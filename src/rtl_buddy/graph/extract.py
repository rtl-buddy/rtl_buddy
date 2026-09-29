# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Runs the optional binding-tier extractor (`rb-graph-extract`) for `rb graph build`.

The extractor is discovered through `rtl_buddy.tool_manifest`. Any failure (missing
tool, non-zero exit, missing or non-node-link output) marks the tier `failed` with a
reason and leaves the other tiers intact. The argv shapes are a contract with the
extractor repo's `docs/extract-contract.md`; change both together.
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

# Manifest name of the extractor package and the binary it installs.
GRAPH_EXTRACT_TOOL = "rtl-buddy-graph-extract"
GRAPH_EXTRACT_BINARY = "rb-graph-extract"

# Subcommand for the deterministic extraction pass.
EXTRACT_VERB = "extract"

# Subcommand that unions node-link graphs; used only as a cross-check of `merge.py`.
MERGE_VERB = "merge-graphs"

# Node-link envelope format.
GRAPH_FORMAT = "node-link"

# Seconds before an extractor subprocess is abandoned.
DEFAULT_TIMEOUT = 900

# Input suffixes by tree.
VERIF_SUFFIXES = (".py",)
SPEC_SUFFIXES = (".md",)

# Directories skipped when collecting extractor inputs.
_SKIP_DIRS = frozenset(
    {".git", "__pycache__", "artefacts", "obj_dir", "node_modules", "venv", ".venv"}
)


@dataclass
class ExtractResult:
    """Outcome of one extractor invocation.

    `ok` is True when the graph was produced and parsed. `graph` is the parsed node-link
    payload when `ok`, `detail` the reason when not, and `cmd` the argv executed (empty
    if nothing ran).
    """

    ok: bool
    graph: dict | None = None
    detail: str | None = None
    cmd: list[str] = dc_field(default_factory=list)


@dataclass(frozen=True)
class ExtractorChoice:
    """The extractor `rb graph build` will run.

    `version` is the probed version, or `"unknown"`; it enters the build fingerprint so
    an upgrade invalidates the cached build.
    """

    executable: str
    version: str


def resolve_extractor(root_cfg=None) -> ExtractorChoice | None:
    """Return the extractor when installed, else None (tier skipped)."""
    spec = next(
        (s for s in get_manifest(root_cfg) if s.name == GRAPH_EXTRACT_TOOL), None
    )
    if spec is None:  # pragma: no cover - the manifest always carries it
        return None
    status = check_tool(spec)
    if status.status == "missing":
        return None
    return ExtractorChoice(GRAPH_EXTRACT_BINARY, status.version or "unknown")


def collect_inputs(
    verif_dir: str | os.PathLike | None, spec_dir: str | os.PathLike | None
) -> list[str]:
    """Return sorted absolute paths of the verif Python and spec markdown files the
    extractor reads.

    RTL is left to the design tier.
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
    """Return argv for the extraction pass."""
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
    """Return argv for the `merge-graphs` cross-check."""
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
    """Run `cmd`, append its output to `log_path`, and return (returncode, last output
    line).

    Uses `run_managed_process` so a hung extractor cannot strand a child or hang the
    build. Returns 127 when the binary is missing and 124 on timeout.
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
        except OSError:  # pragma: no cover - the log is best effort
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
    """Run the extractor over `inputs`, writing `output`.

    Never raises; failures are reported through `ExtractResult`.
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
    """Compare the extractor's `merge-graphs` result with the internal union `internal`.

    Returns a status dict stored in `graph-meta.json` under `merge.extract_cross_check`.
    The internal merge is the one that ships.
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
    # Capped so a wholesale disagreement cannot bloat the sidecar.
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
