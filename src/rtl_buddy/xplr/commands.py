"""Command-layer logic for the ``rb xplr`` CLI surface.

The handlers in ``rtl_buddy.py`` stay thin; everything that decides
*what* a command does lives here so it can be tested without Typer.

Inputs are JSON documents (``--json <file|->``) because the primary
consumer is an agent, not a human. Each command's input shape is
checked explicitly (unknown keys fail loudly with the allowed-key
list), then the resulting record is validated against the strict P0
schema before anything touches disk — an agent that sends a malformed
manifest gets a message naming exactly what was wrong.

Source pinning (P2): when the agent declares ``source.git_sha`` it is
taken verbatim — the agent owns that pin. Otherwise the cfg-xplr commit
policy applies (:func:`rtl_buddy.xplr.gitprov.pin_with_policy`): in the
default ``auto`` mode a dirty source scope is snapshotted to an
``exp/<id>`` branch without disturbing the user's tree, a clean scope
just records ``HEAD``; ``self-managed`` mode requires the user to have
committed. Either way the recorded sha is exact, and ``diff_from``
records the baseline so two experiments can be diffed at the RTL level.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config.xplr import XplrConfig, load_xplr_config
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from . import gitprov, ledger
from .schema import ABSENT, SCHEMA_VERSION, ExperimentRecord

logger = logging.getLogger(__name__)

STATUSES = ("pending", "running", "success", "failed")
TERMINAL_STATUSES = ("success", "failed")

_REGISTER_KEYS = (
    "knobs",
    "hypothesis",
    "parent",
    "config_snapshot",
    "source",
    "provenance",
)
_REGISTER_SOURCE_KEYS = ("git_sha", "branch", "diff_from")
_REGISTER_PROVENANCE_KEYS = ("tools", "agent")
_ATTACH_KEYS = ("status", "metrics", "metric_meta", "artifacts", "provenance")
_ATTACH_PROVENANCE_KEYS = ("tools", "reused_state")


# ---------------------------------------------------------------------------
# JSON input
# ---------------------------------------------------------------------------


def load_json_doc(json_arg: str, *, cwd: Path, what: str) -> dict[str, Any]:
    """Read the ``--json`` argument: a file path, or ``-`` for stdin.

    Returns the parsed JSON object; raises :class:`FatalRtlBuddyError`
    for a missing file, malformed JSON, or a non-object document.
    """

    if json_arg == "-":
        text = sys.stdin.read()
        origin = "stdin"
    else:
        path = Path(json_arg)
        if not path.is_absolute():
            path = cwd / path
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise FatalRtlBuddyError(
                f"{what}: JSON input file not found: {path}"
            ) from None
        origin = str(path)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FatalRtlBuddyError(
            f"{what}: input from {origin} is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc
    if not isinstance(data, dict):
        raise FatalRtlBuddyError(
            f"{what}: input must be a JSON object, got {type(data).__name__}"
        )
    return data


def _check_keys(doc: Any, allowed: tuple[str, ...], where: str) -> None:
    if not isinstance(doc, dict):
        raise FatalRtlBuddyError(
            f"{where} must be a JSON object, got {type(doc).__name__}"
        )
    unknown = sorted(set(doc) - set(allowed))
    if unknown:
        raise FatalRtlBuddyError(
            f"{where}: unknown key(s) {', '.join(repr(k) for k in unknown)}; "
            f"allowed keys: {', '.join(allowed)}"
        )


# ---------------------------------------------------------------------------
# source pinning (P2: cfg-xplr commit policy; plumbing lives in gitprov)
# ---------------------------------------------------------------------------


def _git(project_root: Path, *args: str) -> str | None:
    """Run a git query in ``project_root``; None on any failure."""

    result = subprocess.run(
        ["git", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def pin_source(
    project_root: Path,
    declared: dict[str, Any],
    *,
    exp_id: str,
    cfg: XplrConfig,
    baseline: str | None = None,
    parent_sha: str | None = None,
    ledger_root: Path | None = None,
) -> dict[str, Any]:
    """Resolve the ``source`` block for a new experiment.

    An agent-declared ``git_sha`` is taken verbatim (with optional
    ``branch``/``diff_from``) — the agent owns that pin, and no
    ``dirty`` bit is recorded since the working tree says nothing about
    an arbitrary sha. Otherwise the cfg-xplr commit policy applies
    (see :func:`rtl_buddy.xplr.gitprov.pin_with_policy`): the recorded
    sha is exact in every mode, ``diff_from`` defaults to the parent
    experiment's pinned sha (HEAD-before-snapshot otherwise), and rb
    bookkeeping (``ledger_root``, worktrees, the rb log) is excluded
    from the dirtiness check and any snapshot.
    """

    _check_keys(declared, _REGISTER_SOURCE_KEYS, "register: 'source'")
    if "git_sha" in declared:
        source = dict(declared)
        if baseline is not None:
            resolved = gitprov.resolve_ref(project_root, baseline)
            if "diff_from" in source and source["diff_from"] != resolved:
                raise FatalRtlBuddyError(
                    f"register: --baseline resolves to {resolved} but the "
                    "manifest declares source.diff_from "
                    f"{source['diff_from']!r} — drop one of them"
                )
            source["diff_from"] = resolved
        return source

    return gitprov.pin_with_policy(
        project_root,
        exp_id,
        cfg,
        declared_branch=declared.get("branch"),
        declared_diff_from=declared.get("diff_from"),
        baseline=baseline,
        parent_sha=parent_sha,
        ledger_root=ledger_root,
    )


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def source_diff(
    project_root: Path,
    source_a: dict[str, Any],
    source_b: dict[str, Any],
    *,
    patch: bool = False,
) -> dict[str, Any]:
    """The ``source`` block of ``rb xplr diff``: both refs + the git diff.

    Runs ``git diff --stat <shaA>..<shaB>`` (plus ``-p`` with ``patch``)
    in the project root. When either sha is unknown to the repo — a
    pinned ref from another clone, a shallow checkout, no git at all —
    the diff degrades gracefully to a ``note`` instead of failing.
    """

    sha_a, sha_b = source_a["git_sha"], source_b["git_sha"]
    out: dict[str, Any] = {"a": dict(source_a), "b": dict(source_b)}
    if sha_a == sha_b:
        out["stat"] = ""
        out["note"] = "both experiments pin the same source revision"
        if patch:
            out["patch"] = ""
        return out
    stat = _git(project_root, "diff", "--stat", f"{sha_a}..{sha_b}")
    if stat is None:
        out["stat"] = None
        out["note"] = (
            f"git diff {sha_a}..{sha_b} failed in {project_root} — one or "
            "both pinned revisions are unknown to this repository"
        )
        return out
    out["stat"] = stat.rstrip("\n")
    if patch:
        diff = _git(project_root, "diff", f"{sha_a}..{sha_b}")
        out["patch"] = diff if diff is not None else None
    return out


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def register_experiment(
    root: Path,
    doc: dict[str, Any],
    *,
    project_root: Path,
    cfg: XplrConfig | None = None,
    baseline: str | None = None,
) -> tuple[ExperimentRecord, Path]:
    """Open a new experiment from a register manifest.

    Allocates the next ``exp-NNNN`` id, pins the source under the
    cfg-xplr commit policy (loaded from the project root when ``cfg``
    is not given), sets ``outcome.status = "pending"`` and
    ``provenance.created`` = now, validates the assembled record
    against the schema, and writes it. The disk backstop runs first:
    over the high watermark gc is triggered by policy, and the hard
    cap blocks the new run only when gc could not free enough.
    """

    _check_keys(doc, _REGISTER_KEYS, "register")
    prov_in = doc.get("provenance", {})
    _check_keys(prov_in, _REGISTER_PROVENANCE_KEYS, "register: 'provenance'")
    if cfg is None:
        cfg = load_xplr_config(project_root)

    gitprov.enforce_disk_backstop(project_root, root, cfg)
    gitprov.warn_if_ledger_not_ignored(project_root, root)

    exp_id = ledger.next_id(root)
    parent_sha: str | None = None
    parent_id = doc.get("parent")
    if isinstance(parent_id, str):
        try:  # the schema allows any string parent; only ledger ids resolve
            parent_known = ledger.record_path(root, parent_id).is_file()
        except FatalRtlBuddyError:
            parent_known = False
        if parent_known:
            parent_sha = ledger.read_record(root, parent_id).source.git_sha
    source = pin_source(
        project_root,
        doc.get("source", {}),
        exp_id=exp_id,
        cfg=cfg,
        baseline=baseline,
        parent_sha=parent_sha,
        ledger_root=root,
    )

    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": exp_id,
    }
    if "parent" in doc:
        data["parent"] = doc["parent"]
    if "hypothesis" in doc:
        data["hypothesis"] = doc["hypothesis"]
    data["source"] = source
    data["knobs"] = doc.get("knobs", [])
    if "config_snapshot" in doc:
        data["config_snapshot"] = doc["config_snapshot"]
    data["outcome"] = {"status": "pending"}
    provenance: dict[str, Any] = {"created": _now()}
    if "tools" in prov_in:
        provenance["tools"] = prov_in["tools"]
    if "agent" in prov_in:
        provenance["agent"] = prov_in["agent"]
    data["provenance"] = provenance

    record = ExperimentRecord.from_dict(data)  # strict schema validation
    path = ledger.write_record(root, record)
    log_event(
        logger,
        logging.INFO,
        "xplr.registered",
        id=record.id,
        path=path,
        git_sha=record.source.git_sha,
        n_knobs=len(record.knobs),
    )
    return record, path


def attach_outcome(
    root: Path, exp_id: str, doc: dict[str, Any], *, force: bool = False
) -> tuple[ExperimentRecord, Path]:
    """Attach a flow-declared outcome to an existing experiment.

    ``status`` must be terminal (``success``/``failed``); re-attaching
    to an already-terminal experiment requires ``force``. The merged
    record is validated against the schema before it is written.
    """

    record, path = get_experiment(root, exp_id)
    _check_keys(doc, _ATTACH_KEYS, "attach-outcome")
    status = doc.get("status")
    if status not in TERMINAL_STATUSES:
        raise FatalRtlBuddyError(
            "attach-outcome: 'status' must be one of "
            f"{', '.join(repr(s) for s in TERMINAL_STATUSES)}, got {status!r}"
        )
    current = record.outcome.status
    if current in TERMINAL_STATUSES and not force:
        raise FatalRtlBuddyError(
            f"experiment '{exp_id}' already has a terminal outcome "
            f"(status '{current}') — pass --force to overwrite it"
        )
    prov_in = doc.get("provenance", {})
    _check_keys(prov_in, _ATTACH_PROVENANCE_KEYS, "attach-outcome: 'provenance'")

    data = record.to_dict()
    outcome: dict[str, Any] = {"status": status}
    for key in ("metrics", "metric_meta", "artifacts"):
        if key in doc:
            outcome[key] = doc[key]
    data["outcome"] = outcome
    if "tools" in prov_in:
        existing = data["provenance"].get("tools", [])
        data["provenance"]["tools"] = existing + [
            t for t in prov_in["tools"] if t not in existing
        ]
    if "reused_state" in prov_in:
        data["provenance"]["reused_state"] = prov_in["reused_state"]

    updated = ExperimentRecord.from_dict(data)  # strict schema validation
    path = ledger.write_record(root, updated)
    log_event(
        logger,
        logging.INFO,
        "xplr.outcome_attached",
        id=exp_id,
        status=status,
        path=path,
        forced=force,
    )
    return updated, path


def get_experiment(root: Path, exp_id: str) -> tuple[ExperimentRecord, Path]:
    """Load one experiment; unknown ids fail with the known-id list."""

    path = ledger.record_path(root, exp_id)
    if not path.is_file():
        known = (
            sorted(
                e.name
                for e in root.iterdir()
                if e.is_dir() and e.name not in ledger.RESERVED_DIRNAMES
            )
            if root.is_dir()
            else []
        )
        hint = (
            f"; known experiments: {', '.join(known)}"
            if known
            else "; the ledger is empty — run `rb xplr register` first"
        )
        raise FatalRtlBuddyError(f"unknown experiment id '{exp_id}'{hint}")
    return ledger.read_record(root, exp_id), path


def list_experiments(
    root: Path, *, status: str | None = None
) -> list[ExperimentRecord]:
    """All ledger records, optionally filtered by outcome status."""

    if status is not None and status not in STATUSES:
        raise FatalRtlBuddyError(
            f"invalid --status filter {status!r}: must be one of {', '.join(STATUSES)}"
        )
    records = ledger.list_records(root)
    if status is not None:
        records = [r for r in records if r.outcome.status == status]
    return records


def summarize(record: ExperimentRecord) -> dict[str, Any]:
    """The ``rb xplr list`` summary row for one record."""

    out: dict[str, Any] = {
        "id": record.id,
        "status": record.outcome.status,
        "git_sha": record.source.git_sha,
        "n_knobs": len(record.knobs),
        "created": record.provenance.created,
    }
    if record.hypothesis is not ABSENT:
        out["hypothesis"] = record.hypothesis
    return out
