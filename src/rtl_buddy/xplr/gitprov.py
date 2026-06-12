"""Git-pinned source provenance, worktree isolation, and GC (P2, #298).

The L0 layer of an experiment is its **source revision**. This module
makes it a first-class, reproducible knob:

* :func:`pin_with_policy` — the commit policy behind ``rb xplr
  register``. ``auto`` mode (the default, and the recommended one): a
  dirty tree gets its configured source scope snapshotted to an
  ``exp/<id>`` branch via plumbing (temporary ``GIT_INDEX_FILE`` +
  ``commit-tree``), so the user's working tree, index, and current
  branch are never disturbed; a clean tree just records ``HEAD`` — the
  two paths converge and no redundant commit is created. rb's own
  bookkeeping (the xplr ledger dir, the worktree root, and the rb log
  file) is always excluded from both the dirtiness check and the
  snapshot (:func:`bookkeeping_excludes`), gitignored or not — only
  the user's source decides whether a new sha is minted.
  ``self-managed`` mode requires a clean scope and records ``HEAD``.
* :func:`materialize` / :func:`release` — build each RTL variant in a
  disposable git worktree at its pinned sha (default under the
  configured ``worktree-root``, which must be gitignored; the default
  lives under ``artefacts/``). The branch is the durable artifact, the
  worktree is not. The worktree path lives in a
  ``artefacts/xplr/<exp>/worktree.json`` sidecar — the record schema
  stays frozen.
* :func:`gc` — frontier-aware, **non-interactive** disk reclamation.
  Eviction removes the heavy artifacts (worktree + the files listed in
  ``outcome.artifacts``), never ``record.json``: source is git-pinned,
  so an evicted experiment can always be re-materialized and replayed
  from its recorded ``config_snapshot``.

All git operations run via subprocess against the project-root repo.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config.xplr import EVICTION_POLICIES, XplrConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import DEFAULT_FILE_LOG, log_event
from . import analysis, ledger
from .schema import ABSENT, ExperimentRecord


logger = logging.getLogger(__name__)

GB = 1024**3
WORKTREE_SIDECAR = "worktree.json"
SNAPSHOT_REF_PREFIX = "refs/heads/exp/"


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run one git command in ``repo``; fail loudly unless ``check=False``."""

    full_env = None
    if env is not None:
        full_env = dict(os.environ)
        full_env.update(env)
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=full_env,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
        raise FatalRtlBuddyError(f"git {' '.join(args)} failed in {repo}: {detail}")
    return result


def head_sha(project_root: Path) -> str | None:
    """HEAD's full sha, or None when not a git repo / no commits yet."""

    result = _git(project_root, "rev-parse", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def current_branch(project_root: Path) -> str | None:
    """The checked-out branch name, or None when detached / no repo."""

    result = _git(project_root, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return None if name == "HEAD" else name  # "HEAD" == detached


def resolve_ref(project_root: Path, ref: str) -> str:
    """Resolve ``ref`` to a full commit sha; fail loudly when unknown."""

    result = _git(project_root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    return result.stdout.strip()


def _repo_relative(project_root: Path, path: Path) -> Path | None:
    """``path`` relative to the project root, or None when outside it."""

    try:
        return path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return None


def bookkeeping_excludes(
    project_root: Path, ledger_root: Path | None, cfg: XplrConfig
) -> list[str]:
    """Pathspecs that keep rb bookkeeping out of dirt checks and snapshots.

    The xplr ledger dir (records, locks, prior experiments' artifacts),
    the worktree root, and the rb log file are bookkeeping, not source.
    Snapshotting them would mint a fresh ``git_sha`` on every register
    even when the RTL is identical (breaking the "same source revision"
    signal of ``xplr diff``), embed every prior record into each
    ``exp/<id>`` branch (growing without bound), and leak stale ledger
    copies into materialized worktrees — so they are excluded with
    ``:(exclude)`` pathspec magic whether or not they are gitignored.

    Paths git already ignores are skipped: ``status``/``add -A`` never
    pick them up, and ``git add`` refuses a pathspec — even an exclude
    one — that literally names an ignored path.
    """

    candidates = [cfg.worktree_dir(project_root), project_root / DEFAULT_FILE_LOG]
    if ledger_root is not None:
        candidates.insert(0, ledger_root)
    excludes: list[str] = []
    for path in candidates:
        rel = _repo_relative(project_root, path)
        if rel is None:
            continue
        ignored = _git(project_root, "check-ignore", "-q", str(path), check=False)
        if ignored.returncode == 0:
            continue
        excludes.append(f":(exclude){rel.as_posix()}")
    return excludes


def warn_if_ledger_not_ignored(project_root: Path, ledger_root: Path) -> None:
    """Register-time hygiene warning, mirroring ``xplr.worktree_not_ignored``.

    Snapshots and dirt checks already exclude rb bookkeeping
    (:func:`bookkeeping_excludes`), but a ledger dir or rb log file
    that is inside the repo and not gitignored still clutters ``git
    status`` and gets swept into the user's own commits — warn once
    per offending path with the fix spelled out.
    """

    if head_sha(project_root) is None:
        return  # not a git repo: nothing to ignore
    for target in (ledger_root, project_root / DEFAULT_FILE_LOG):
        if _repo_relative(project_root, target) is None or not target.exists():
            continue
        result = _git(project_root, "check-ignore", "-q", str(target), check=False)
        if result.returncode != 0:
            log_event(
                logger,
                logging.WARNING,
                "xplr.ledger_not_ignored",
                path=target,
                hint="add it to .gitignore (the default artefacts/ ledger "
                "location and rtl_buddy.log) — rb excludes its bookkeeping "
                "from snapshots automatically, but your own commits and "
                "git status will pick it up",
            )


def _scope_dirty(
    project_root: Path, scope: list[str], excludes: list[str] | None = None
) -> bool:
    """True when ``git status --porcelain`` reports changes inside scope."""

    result = _git(
        project_root, "status", "--porcelain", "--", *scope, *(excludes or [])
    )
    return bool(result.stdout.strip())


def _commit_ident_env(project_root: Path) -> dict[str, str]:
    """Author/committer env fallback so snapshots never stall on identity.

    A repo without ``user.name``/``user.email`` would make
    ``commit-tree`` fail and stall the non-interactive agent loop; the
    snapshot commit is rb-internal bookkeeping, so a neutral fallback
    identity is used instead of erroring. A configured identity always
    wins.
    """

    env: dict[str, str] = {}
    name = _git(project_root, "config", "user.name", check=False).stdout.strip()
    email = _git(project_root, "config", "user.email", check=False).stdout.strip()
    if not name:
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "rb xplr"
    if not email:
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "rb-xplr@localhost"
    return env


# ---------------------------------------------------------------------------
# commit policy (register's L0 layer)
# ---------------------------------------------------------------------------


def _existing_snapshot(project_root: Path, tree: str, base_sha: str) -> str | None:
    """An existing ``exp/*`` snapshot commit with this tree off this base.

    Lets :func:`snapshot_scope` reuse the prior commit when the scoped
    source is byte-identical (e.g. two registers probing flow-layer
    knobs over the same dirty RTL): identical source must pin an
    identical sha, or "did the source actually change?" is unanswerable
    from the ledger.
    """

    result = _git(
        project_root,
        "for-each-ref",
        "--format=%(objectname) %(tree) %(parent)",
        f"{SNAPSHOT_REF_PREFIX}*",
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == tree and parts[2] == base_sha:
            return parts[0]
    return None


def snapshot_scope(
    project_root: Path,
    exp_id: str,
    scope: list[str],
    base_sha: str,
    *,
    excludes: list[str] | None = None,
) -> str | None:
    """Snapshot the source scope to an ``exp/<exp_id>`` branch off ``base_sha``.

    Implemented with plumbing so the user's working tree, index, and
    checked-out branch are untouched: a temporary ``GIT_INDEX_FILE``
    is seeded from ``base_sha`` (``read-tree``), the scoped working-tree
    state is staged into it (``add -A -- <scope>``, which also picks up
    untracked files and deletions), and the resulting tree is committed
    with ``commit-tree`` and pointed at by ``refs/heads/exp/<exp_id>``.
    ``excludes`` (``:(exclude)`` pathspecs, see
    :func:`bookkeeping_excludes`) are never staged.

    Returns the snapshot commit sha — or None when the scoped tree is
    identical to ``base_sha`` (dirtiness outside the scope), in which
    case no commit and no branch are created. An existing ``exp/*``
    snapshot with the same tree off the same base is reused (the new
    branch points at it), so registering twice with identical RTL pins
    the same sha and ``xplr diff`` can say so.
    """

    tmp_dir = tempfile.mkdtemp(prefix="rb-xplr-index-")
    try:
        env = {"GIT_INDEX_FILE": str(Path(tmp_dir) / "index")}
        _git(project_root, "read-tree", base_sha, env=env)
        _git(project_root, "add", "-A", "--", *scope, *(excludes or []), env=env)
        tree = _git(project_root, "write-tree", env=env).stdout.strip()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    base_tree = _git(project_root, "rev-parse", f"{base_sha}^{{tree}}").stdout.strip()
    if tree == base_tree:
        return None
    commit = _existing_snapshot(project_root, tree, base_sha)
    reused = commit is not None
    if commit is None:
        commit = _git(
            project_root,
            "commit-tree",
            tree,
            "-p",
            base_sha,
            "-m",
            f"rb xplr: source snapshot for {exp_id}",
            env=_commit_ident_env(project_root),
        ).stdout.strip()
    _git(project_root, "update-ref", f"{SNAPSHOT_REF_PREFIX}{exp_id}", commit)
    log_event(
        logger,
        logging.INFO,
        "xplr.source_snapshotted",
        id=exp_id,
        sha=commit,
        branch=f"exp/{exp_id}",
        base=base_sha,
        scope=scope,
        reused=reused,
    )
    return commit


def pin_with_policy(
    project_root: Path,
    exp_id: str,
    cfg: XplrConfig,
    *,
    declared_branch: str | None = None,
    declared_diff_from: str | None = None,
    baseline: str | None = None,
    parent_sha: str | None = None,
    ledger_root: Path | None = None,
) -> dict[str, Any]:
    """Pin the source for a new experiment under the configured policy.

    Returns the record's ``source`` block. ``diff_from`` resolution
    order: explicit ``--baseline`` ref (resolved against the repo) ==
    declared ``source.diff_from`` (taken verbatim; the two may not
    disagree), else the parent experiment's pinned sha, else
    HEAD-before-snapshot — so the RTL-level diff of #299 is always
    well-defined. rb bookkeeping (``ledger_root``, the worktree root,
    the rb log file) never counts as source: it is excluded from both
    the dirtiness check and any auto-commit snapshot.
    """

    head = head_sha(project_root)
    if head is None:
        raise FatalRtlBuddyError(
            "register: no source.git_sha declared and the project root "
            f"({project_root}) is not a git repository with commits — "
            "pass source.git_sha in the --json manifest"
        )
    diff_from = head if parent_sha is None else parent_sha
    if baseline is not None:
        resolved = resolve_ref(project_root, baseline)
        if declared_diff_from is not None and declared_diff_from != resolved:
            raise FatalRtlBuddyError(
                f"register: --baseline resolves to {resolved} but the "
                f"manifest declares source.diff_from {declared_diff_from!r} "
                "— drop one of them"
            )
        diff_from = resolved
    elif declared_diff_from is not None:
        diff_from = declared_diff_from

    scope = list(cfg.source_scope)
    excludes = bookkeeping_excludes(project_root, ledger_root, cfg)
    dirty = _scope_dirty(project_root, scope, excludes)
    if cfg.commit_mode == "self-managed" and dirty:
        raise FatalRtlBuddyError(
            "register: the working tree has uncommitted changes inside the "
            f"source scope ({', '.join(scope)}) and cfg-xplr commit-mode is "
            "'self-managed' — commit your changes first (or switch to the "
            "recommended commit-mode 'auto', which snapshots the scope to an "
            "exp/<id> branch for you)"
        )

    snapshot = None
    if dirty:  # auto mode: snapshot the scope, leave the user's tree alone
        snapshot = snapshot_scope(project_root, exp_id, scope, head, excludes=excludes)

    source: dict[str, Any] = {}
    if snapshot is not None:
        source["git_sha"] = snapshot
        source["branch"] = (
            declared_branch if declared_branch is not None else f"exp/{exp_id}"
        )
    else:
        source["git_sha"] = head
        branch = (
            declared_branch
            if declared_branch is not None
            else current_branch(project_root)
        )
        if branch is not None:
            source["branch"] = branch
    source["diff_from"] = diff_from
    # the pinned sha is exact by construction in every mode: either the
    # scope was clean (HEAD == the scoped tree) or it was snapshotted
    source["dirty"] = False
    return source


# ---------------------------------------------------------------------------
# worktree isolation
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sidecar_path(ledger_root: Path, exp_id: str) -> Path:
    """``artefacts/xplr/<exp>/worktree.json`` — schema stays frozen."""

    return ledger.record_path(ledger_root, exp_id).parent / WORKTREE_SIDECAR


def read_sidecar(ledger_root: Path, exp_id: str) -> dict[str, Any] | None:
    """The parsed worktree sidecar, or None when absent/malformed."""

    path = sidecar_path(ledger_root, exp_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and "path" in data else None


def _warn_if_not_ignored(project_root: Path, worktree: Path) -> None:
    """A worktree inside the repo that isn't gitignored dirties the tree."""

    try:
        worktree.relative_to(project_root)
    except ValueError:
        return  # outside the repo: nothing to ignore
    result = _git(project_root, "check-ignore", "-q", str(worktree), check=False)
    if result.returncode != 0:
        log_event(
            logger,
            logging.WARNING,
            "xplr.worktree_not_ignored",
            path=worktree,
            hint="add the worktree root (default artefacts/) to .gitignore "
            "or auto-commit snapshots will pick the worktree up",
        )


def materialize(
    project_root: Path,
    ledger_root: Path,
    record: ExperimentRecord,
    cfg: XplrConfig,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    """Create (or reuse) the experiment's worktree at its pinned sha.

    Idempotent: when the sidecar already points at a live worktree the
    existing one is returned (``reused: true``); an explicit ``--path``
    that disagrees with it fails loudly rather than silently growing a
    second copy. Stale sidecars (worktree dir removed out-of-band) are
    pruned and re-created.
    """

    existing = read_sidecar(ledger_root, record.id)
    if existing is not None:
        worktree = Path(existing["path"])
        if (worktree / ".git").exists():
            if path is not None and path.resolve() != worktree.resolve():
                raise FatalRtlBuddyError(
                    f"experiment '{record.id}' is already materialized at "
                    f"{worktree} — release it first if you want it at {path}"
                )
            log_event(
                logger,
                logging.INFO,
                "xplr.worktree_reused",
                id=record.id,
                path=worktree,
            )
            return {**existing, "reused": True}
        _git(project_root, "worktree", "prune", check=False)  # stale sidecar

    worktree = path if path is not None else cfg.worktree_dir(project_root) / record.id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(
        project_root,
        "worktree",
        "add",
        "--detach",
        str(worktree),
        record.source.git_sha,
    )
    _warn_if_not_ignored(project_root, worktree)
    info = {
        "id": record.id,
        "git_sha": record.source.git_sha,
        "path": str(worktree),
        "created": _now(),
    }
    sidecar = sidecar_path(ledger_root, record.id)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    log_event(
        logger,
        logging.INFO,
        "xplr.worktree_materialized",
        id=record.id,
        path=worktree,
        sha=record.source.git_sha,
    )
    return {**info, "reused": False}


def release(project_root: Path, ledger_root: Path, exp_id: str) -> dict[str, Any]:
    """Remove the experiment's worktree; the branch (if any) is kept.

    Idempotent: releasing an experiment with no live worktree succeeds
    with ``removed: false`` (stale state is still pruned).
    """

    info = read_sidecar(ledger_root, exp_id)
    worktree = Path(info["path"]) if info is not None else None
    removed = False
    if worktree is not None and worktree.exists():
        result = _git(
            project_root, "worktree", "remove", "--force", str(worktree), check=False
        )
        if result.returncode != 0 and worktree.exists():
            # e.g. registered against another clone; reclaim the disk anyway
            shutil.rmtree(worktree)
        removed = True
    _git(project_root, "worktree", "prune", check=False)
    sidecar_path(ledger_root, exp_id).unlink(missing_ok=True)
    log_event(
        logger,
        logging.INFO,
        "xplr.worktree_released",
        id=exp_id,
        path=worktree,
        removed=removed,
    )
    return {
        "id": exp_id,
        "path": str(worktree) if worktree else None,
        "removed": removed,
    }


# ---------------------------------------------------------------------------
# frontier-aware gc
# ---------------------------------------------------------------------------


def _tree_bytes(path: Path) -> int:
    """Recursive apparent size of ``path`` (files + symlink entries)."""

    if not path.exists():
        return 0
    if path.is_file() or path.is_symlink():
        return path.lstat().st_size
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            entry = Path(dirpath) / name
            try:
                total += entry.lstat().st_size
            except OSError:  # pragma: no cover - racing deletion
                continue
    return total


def _worktree_of(
    ledger_root: Path, exp_id: str, cfg: XplrConfig, project_root: Path
) -> Path | None:
    info = read_sidecar(ledger_root, exp_id)
    if info is not None:
        return Path(info["path"])
    default = cfg.worktree_dir(project_root) / exp_id
    return default if default.exists() else None


def total_usage_bytes(project_root: Path, ledger_root: Path, cfg: XplrConfig) -> int:
    """Disk usage of every experiment dir plus its worktree, in bytes."""

    total = 0
    if not ledger_root.is_dir():
        return 0
    for entry in ledger_root.iterdir():
        if not entry.is_dir() or entry == cfg.worktree_dir(project_root):
            continue
        total += _tree_bytes(entry)
        worktree = _worktree_of(ledger_root, entry.name, cfg, project_root)
        if worktree is not None:
            total += _tree_bytes(worktree)
    return total


def _protected_ids(
    records: list[ExperimentRecord],
) -> tuple[set[str], str | None]:
    """Frontier members plus their direct lineage (parent chains).

    Returns ``(ids, note)``; when the frontier cannot be computed (no
    success with directed numeric metrics yet) nothing is
    frontier-protected and the note says why — eviction is still safe
    because every record keeps its pinned sha.
    """

    try:
        frontier = analysis.pareto_frontier(records)["frontier"]
    except FatalRtlBuddyError as exc:
        return set(), f"frontier not computable ({exc}); no frontier protection"
    by_id = {record.id: record for record in records}
    protected: set[str] = set()
    for member in frontier:
        cursor: str | None = member["id"]
        while cursor is not None and cursor in by_id and cursor not in protected:
            protected.add(cursor)
            parent = by_id[cursor].parent
            cursor = parent if isinstance(parent, str) else None
    return protected, None


def _artifact_eviction_targets(
    record: ExperimentRecord, exp_dir: Path, project_root: Path
) -> list[Path]:
    """The heavy artifact paths safe to delete for one experiment.

    Only paths that resolve **inside the experiment dir** are eligible
    (relative entries are tried against the project root, then the
    experiment dir); ``record.json`` and the worktree sidecar are never
    candidates — the ledger record is permanent.
    """

    artifacts = record.outcome.artifacts
    if artifacts is ABSENT:
        return []
    exp_dir = exp_dir.resolve()
    targets: list[Path] = []
    for entry in artifacts:
        raw = Path(entry)
        candidates = [raw] if raw.is_absolute() else [project_root / raw, exp_dir / raw]
        for candidate in candidates:
            resolved = candidate.resolve()
            if not resolved.exists():
                continue
            if not resolved.is_relative_to(exp_dir):
                continue
            if resolved.name in (ledger.RECORD_FILENAME, WORKTREE_SIDECAR):
                continue
            targets.append(resolved)
            break
    return targets


def gc(
    project_root: Path,
    ledger_root: Path,
    cfg: XplrConfig,
    *,
    policy: str | None = None,
    target_gb: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Reclaim experiment disk space, frontier-first-protected, non-interactive.

    Measures every experiment dir + worktree; when usage exceeds the
    target (``--target-gb`` or the configured high watermark), evicts
    heavy artifacts per policy until under target:

    * ``keep-frontier`` (default): frontier members and their direct
      lineage are never auto-evicted; everything else (dominated,
      failed, superseded) goes oldest-first.
    * ``oldest-first``: no frontier protection, oldest-first.
    * ``manual``: only lists the candidates; evicts nothing.

    Non-terminal (pending/running) experiments are never evicted — a
    flow may be writing into them right now. Eviction removes the
    worktree and the ``outcome.artifacts`` files inside the experiment
    dir; ``record.json`` (and the exp branch + sha) always survive, so
    any evicted experiment can be re-materialized and replayed.
    """

    policy = policy if policy is not None else cfg.eviction_policy
    if policy not in EVICTION_POLICIES:
        raise FatalRtlBuddyError(
            f"invalid gc policy {policy!r}: must be one of "
            f"{', '.join(EVICTION_POLICIES)}"
        )
    if target_gb is not None and target_gb < 0:
        raise FatalRtlBuddyError("--target-gb must be non-negative")
    target_bytes = int(
        (target_gb if target_gb is not None else cfg.disk_high_watermark_gb) * GB
    )

    records = ledger.list_records(ledger_root)
    by_id = {record.id: record for record in records}
    usage: dict[str, int] = {}
    for record in records:
        exp_dir = ledger_root / record.id
        worktree = _worktree_of(ledger_root, record.id, cfg, project_root)
        usage[record.id] = _tree_bytes(exp_dir) + (
            _tree_bytes(worktree) if worktree is not None else 0
        )
    usage_before = sum(usage.values())

    notes: list[str] = []
    protected: set[str] = set()
    if policy == "keep-frontier":
        protected, note = _protected_ids(records)
        if note is not None:
            notes.append(note)
    nonterminal = {
        record.id
        for record in records
        if record.outcome.status not in ("success", "failed")
    }

    def created_of(exp_id: str) -> str:
        return by_id[exp_id].provenance.created

    candidates = sorted(
        (
            exp_id
            for exp_id in usage
            if exp_id not in protected and exp_id not in nonterminal
        ),
        key=lambda exp_id: (created_of(exp_id), exp_id),
    )

    evicted: list[dict[str, Any]] = []
    remaining = usage_before
    if remaining > target_bytes and policy != "manual":
        for exp_id in candidates:
            if remaining <= target_bytes:
                break
            record = by_id[exp_id]
            exp_dir = ledger_root / exp_id
            worktree = _worktree_of(ledger_root, exp_id, cfg, project_root)
            targets = _artifact_eviction_targets(record, exp_dir, project_root)
            freed = sum(_tree_bytes(t) for t in targets)
            if worktree is not None:
                freed += _tree_bytes(worktree)
            if freed == 0:
                continue  # nothing heavy here; the record itself is kept
            if not dry_run:
                if worktree is not None:
                    release(project_root, ledger_root, exp_id)
                for target in targets:
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink(missing_ok=True)
                log_event(
                    logger,
                    logging.INFO,
                    "xplr.gc_evicted",
                    id=exp_id,
                    bytes_freed=freed,
                    worktree=worktree,
                    artifacts=[str(t) for t in targets],
                )
            evicted.append(
                {
                    "id": exp_id,
                    "bytes_freed": freed,
                    "worktree_removed": str(worktree) if worktree else None,
                    "artifacts_removed": [str(t) for t in targets],
                }
            )
            remaining -= freed
        if remaining > target_bytes:
            notes.append(
                "eviction could not reach the target: every remaining "
                "experiment is protected (frontier/lineage/non-terminal) or "
                "holds no evictable artifacts"
            )
    elif remaining > target_bytes:  # manual policy: list, never evict
        notes.append(
            "policy 'manual': listed eviction candidates only; nothing evicted"
        )

    bytes_freed_total = sum(entry["bytes_freed"] for entry in evicted)
    usage_after = usage_before if dry_run else usage_before - bytes_freed_total
    payload = {
        "policy": policy,
        "dry_run": dry_run,
        "target_bytes": target_bytes,
        "usage_bytes_before": usage_before,
        "usage_bytes_after": usage_after,
        "bytes_freed_total": bytes_freed_total,
        "evicted": evicted,
        "protected": sorted(protected | nonterminal),
        "candidates": candidates,
        "over_hard_cap": usage_after > int(cfg.disk_hard_cap_gb * GB),
        "notes": notes,
    }
    log_event(
        logger,
        logging.INFO,
        "xplr.gc",
        policy=policy,
        dry_run=dry_run,
        usage_bytes_before=usage_before,
        usage_bytes_after=payload["usage_bytes_after"],
        n_evicted=len(evicted),
        over_hard_cap=payload["over_hard_cap"],
    )
    return payload


def enforce_disk_backstop(
    project_root: Path, ledger_root: Path, cfg: XplrConfig
) -> None:
    """The register-time disk backstop (non-interactive, never prompts).

    Crossing the high watermark triggers gc under the configured policy
    (``manual`` opts out of auto-gc); the hard cap is the backstop that
    blocks the new run only when gc could not free enough. The agent
    loop is never stalled by a prompt — only by a hard error with the
    fix spelled out.
    """

    hard_cap = int(cfg.disk_hard_cap_gb * GB)
    high = int(cfg.disk_high_watermark_gb * GB)
    used = total_usage_bytes(project_root, ledger_root, cfg)
    if used <= high:
        return
    if cfg.eviction_policy != "manual":
        report = gc(project_root, ledger_root, cfg)
        used = report["usage_bytes_after"]
    if used > hard_cap:
        raise FatalRtlBuddyError(
            f"xplr ledger disk usage ({used / GB:.2f} GB) exceeds the hard "
            f"cap ({cfg.disk_hard_cap_gb:g} GB) and gc could not free enough "
            "below it — evict experiments manually (`rb xplr gc`, `rb xplr "
            "release <exp>`) or raise cfg-xplr disk-hard-cap-gb"
        )
