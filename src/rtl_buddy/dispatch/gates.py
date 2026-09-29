# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Gates manifest: which sim job holds which plan index.

The head writes it after submitting a suite; the build job reads it to release
the ``afterok`` dependency of a compile key's sim jobs as soon as that key is
built (see :func:`..slurm.release_dependency`). It is an optimization on top of
plain ``afterok``, so nothing here raises; failures come back as reason strings.
"""

import json
import time
from pathlib import Path

GATES_SCHEMA_VERSION = 1

# Bounds the build job's wait for the head to finish submitting.
GATES_WAIT_S = 120.0
GATES_POLL_S = 2.0


def write_gates(path, *, run_token, entries) -> Path:
    """Write the plan-index to job-id manifest; return ``path``.

    ``entries`` is an iterable of ``(plan index, test name, job id, cluster)``,
    one per submitted (test, run_id), so an index can repeat. ``cluster`` is
    stored per entry (``None`` = local cluster, omitted) because job ids are
    unique only within a cluster. ``run_token`` lets the build job reject a
    manifest left by an earlier run.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": GATES_SCHEMA_VERSION,
        "run_token": run_token,
        "entries": [
            {
                "index": int(index),
                "test": str(test),
                "job_id": str(job_id),
                **({} if cluster is None else {"cluster": str(cluster)}),
            }
            for index, test, job_id, cluster in entries
        ],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)  # atomic: the build job polls this file
    return path


def load_gates(path) -> tuple[dict | None, str | None]:
    """Read the manifest as ``(payload, reason)`` without raising.

    Raising would fail the build job and cancel every dependent sim job.
    """
    try:
        payload = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None, "not written yet"
    except (OSError, ValueError) as e:
        return None, str(e)[:200]
    if not isinstance(payload, dict):
        return None, "gates manifest is not a JSON object"
    version = payload.get("schema_version")
    if version != GATES_SCHEMA_VERSION:
        return None, (
            f"gates manifest has schema_version {version!r}, expected "
            f"{GATES_SCHEMA_VERSION}"
        )
    if not isinstance(payload.get("entries"), list):
        return None, "gates manifest has no `entries` list"
    return payload, None


def wait_for_gates(
    path,
    *,
    run_token=None,
    timeout_s: float | None = None,
    interval_s: float | None = None,
    sleep=time.sleep,
) -> tuple[dict | None, str | None]:
    """Poll for the manifest until it appears; returns ``(payload, reason)``.

    The build job starts before the sims it gates, so an absent file means the
    head is still submitting. After ``timeout_s`` the head is presumed gone and
    the run falls back to plain ``afterok``. A manifest with another run's
    ``run_token`` is rejected immediately.
    """
    timeout = GATES_WAIT_S if timeout_s is None else timeout_s
    interval = GATES_POLL_S if interval_s is None else interval_s
    deadline = time.monotonic() + timeout
    while True:
        payload, reason = load_gates(path)
        if payload is not None:
            found = payload.get("run_token")
            if run_token is not None and found != run_token:
                return None, (
                    f"gates manifest carries run token {found!r}, not this "
                    f"run's {run_token!r} (a manifest left by an earlier run)"
                )
            return payload, None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, reason
        sleep(min(interval, remaining))


def release_batches(payload, indices) -> list[tuple[str | None, list[str]]]:
    """``[(cluster, job ids)]`` for these plan indices, one batch per cluster.

    One index can map to several ids (one per run_id). Order follows ``indices``,
    duplicates are dropped, and malformed entries are skipped. Batches are per
    cluster because ``scontrol`` addresses one cluster at a time.
    """
    by_index: dict[int, list[tuple[str, str | None]]] = {}
    for entry in payload.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        index, job_id = entry.get("index"), entry.get("job_id")
        if not isinstance(index, int) or not isinstance(job_id, str) or not job_id:
            continue
        cluster = entry.get("cluster")
        if cluster is not None and not isinstance(cluster, str):
            # Unknown cluster: fall back to the local cluster.
            cluster = None
        by_index.setdefault(index, []).append((job_id, cluster))
    batches: dict[str | None, list[str]] = {}
    # Dedupe on (cluster, id): the same id can exist on two clusters.
    seen: set[tuple[str | None, str]] = set()
    for index in indices:
        for job_id, cluster in by_index.get(index, ()):
            if (cluster, job_id) in seen:
                continue
            seen.add((cluster, job_id))
            batches.setdefault(cluster, []).append(job_id)
    return list(batches.items())
