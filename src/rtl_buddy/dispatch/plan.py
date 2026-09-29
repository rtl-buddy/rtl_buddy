# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Dispatch plan manifest: the head's one expansion of a suite's runnable tests.

The head writes each :class:`TestConfig` as JSON on the shared filesystem
(via :meth:`TestConfig.to_plan_dict`); the build job and sim jobs read their
configs back instead of re-running the ``sweep`` hook.
"""

import json
import os
from pathlib import Path

from ..config.test import TestConfig
from ..errors import FatalRtlBuddyError

PLAN_SCHEMA_VERSION = 1


def run_token_tag(run_token) -> str:
    """The short, filesystem-safe form of a run token used in filenames."""
    tag = "".join(c for c in str(run_token or "") if c.isalnum())[:8]
    return tag or "notoken"


def run_scoped_path(dispatch_root, prefix: str, run_token, suffix=".json") -> Path:
    """``<dispatch_root>/<prefix>-<pid>-<token-tag><suffix>``, a file owned by one head.

    Names include the run token as well as the pid because pids are reused, so
    a pid-only name could collide with files of a still-queued earlier run.
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

    ``configs`` is the head's ordered expansion of the suite's runnable tests.
    ``run_token`` is a per-invocation nonce that each job stamps into its
    result envelope, so the head can tell this run's envelope from a stale one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "suite_config": suite_config_path,
        "run_token": run_token,
        # A list, not a dict: the build job compiles in this order.
        "tests": [cfg.to_plan_dict() for cfg in configs],
    }
    if master_seed is not None:
        payload["master_seed"] = master_seed
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)  # atomic: jobs never read a partial file
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
    """The head's per-invocation run token, or ``None`` if the plan has none."""
    return _load(path).get("run_token")


def read_plan_master_seed(path: Path) -> int | None:
    """The exact master seed selected by the dispatching head, if any."""
    return _load(path).get("master_seed")


def read_plan_config(path: Path, test_name: str) -> TestConfig | None:
    """One test's config from the plan, or ``None`` if the plan does not list it.

    Callers fall back to hook expansion on ``None``.
    """
    for d in _load(path)["tests"]:
        if d.get("name") == test_name:
            return TestConfig.from_plan_dict(d)
    return None
