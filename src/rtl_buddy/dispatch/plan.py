# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Dispatch plan manifest (#351).

Under ``--dispatch`` the suite's ``sweep`` hook must run exactly once. The
head expands the suite, then writes each runnable :class:`TestConfig` here;
the build job and every sim job rebuild their configs from this file
instead of re-running the hook. That removes the redundant expansions (one
per build job, one per sim job) and — more importantly — makes the head the
single source of truth, so a nondeterministic hook can't expand differently
per process and leave a sim job's compile key unbuilt.

The manifest is JSON on the shared filesystem the head and jobs share, so
no live objects cross the process boundary — only the JSON-safe dicts from
:meth:`TestConfig.to_plan_dict`.
"""

import json
import os
from pathlib import Path

from ..config.test import TestConfig
from ..errors import FatalRtlBuddyError

PLAN_SCHEMA_VERSION = 1


def run_token_tag(run_token) -> str:
    """The short, filesystem-safe half of a run token used in a filename."""
    tag = "".join(c for c in str(run_token or "") if c.isalnum())[:8]
    return tag or "notoken"


def run_scoped_path(dispatch_root, prefix: str, run_token, suffix=".json") -> Path:
    """``<dispatch_root>/<prefix>-<pid>-<token>><suffix>`` for a per-run file.

    Every file under ``.dispatch/`` that one head writes and its own jobs
    read — this plan, the build envelope, the build log, the gates manifest
    (#548), the array scratch directories, the run manifest (#521) — is
    named for the head that owns it. The pid alone was that name until an
    interrupted run became something a later run can find: pids are reused,
    so after a reboot a new head can carry the pid of a run whose fleet is
    still queued, and a pid-keyed name would have it write its plan over
    the one those jobs are still reading. The run token is unique per
    invocation, so the two runs' files coexist.
    """
    return (
        Path(dispatch_root)
        / f"{prefix}-{os.getpid()}-{run_token_tag(run_token)}{suffix}"
    )


def write_plan(
    path: Path,
    suite_config_path: str,
    configs: list[TestConfig],
    run_token: str,
    *,
    master_seed: int | None = None,
) -> Path:
    """Write the dispatch plan for one suite; return ``path``.

    ``configs`` is the head's single, ordered expansion of the suite's
    runnable tests. Names are unique after sweep expansion, so they key the
    manifest for O(1) lookup by a sim job.

    ``run_token`` is a per-invocation nonce the head threads to every sim
    job through this manifest. Each job stamps it into its result envelope
    so the head can tell this run's envelope from a stale one *by identity*
    rather than by absence — the head therefore never pre-unlinks the
    result path, which on NFS would leave a negative dentry that blinds it
    to the job's write for ~acdirmin seconds (#362).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "suite_config": suite_config_path,
        "run_token": run_token,
        # Ordered list (not a dict) so the build job compiles in the head's
        # expansion order; sim-job lookup builds its own index by name.
        "tests": [cfg.to_plan_dict() for cfg in configs],
    }
    if master_seed is not None:
        payload["master_seed"] = master_seed
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)  # atomic: a job never reads a half-written manifest
    return path


def _load(path: Path) -> dict:
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise FatalRtlBuddyError(f"dispatch plan {path} is unreadable: {e}") from e
    version = payload.get("schema_version")
    if version != PLAN_SCHEMA_VERSION:
        raise FatalRtlBuddyError(
            f"dispatch plan {path} has schema_version {version!r}, "
            f"expected {PLAN_SCHEMA_VERSION} (a plan from a different rtl_buddy "
            "version; resubmit the regression)."
        )
    return payload


def read_plan_configs(path: Path) -> list[TestConfig]:
    """All runnable configs from the plan, in the head's expansion order."""
    return [TestConfig.from_plan_dict(d) for d in _load(path)["tests"]]


def read_plan_token(path: Path) -> str | None:
    """The head's per-invocation run token, or ``None`` for a legacy plan.

    A sim job stamps this into its result envelope so the head detects a
    stale envelope by identity instead of by absence (#362).
    """
    return _load(path).get("run_token")


def read_plan_master_seed(path: Path) -> int | None:
    """The exact master seed selected by the dispatching head, if any."""
    return _load(path).get("master_seed")


def read_plan_config(path: Path, test_name: str) -> TestConfig | None:
    """Resolve one test's config from the plan, or ``None`` if absent.

    ``None`` (not an error) lets a sim job whose name is missing fall back
    to hook expansion — the plan is an optimization, not a hard dependency.
    """
    for d in _load(path)["tests"]:
        if d.get("name") == test_name:
            return TestConfig.from_plan_dict(d)
    return None
