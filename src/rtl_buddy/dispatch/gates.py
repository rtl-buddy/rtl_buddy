# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Sim-job gates manifest: which job holds which plan index (#548).

Under Slurm every sim job of a suite is submitted ``--dependency=afterok``
on that suite's *one* build job, so a test whose compile key finished in
the first minute still waits for the slowest key in the plan. The build
job is the only process that knows when a key is done — it is the one
compiling them — but it does not know which jobs are waiting on it: the
head submits the build job *before* the sims, precisely so the keys can be
derived on a compute node from the ``run.f`` it writes there (#458).

This manifest is the missing half of that handshake. The head writes it
next to the plan once the whole suite is submitted; the build job reads it
to turn "the configs at plan indices 3 and 7 are built" into "clear the
dependency of job 1234_2 and 1234_5" (see
:func:`..slurm.release_dependency`).

Everything here is best effort by construction. The manifest is an
optimization on top of a gate that is already correct: a run whose head
died before writing it, or whose ``scontrol`` is missing, keeps exactly
the ``afterok`` behaviour of every release before this one. So nothing in
this module raises — a manifest that cannot be read is a reason string,
and the caller logs it and builds on.
"""

import json
import time
from pathlib import Path

GATES_SCHEMA_VERSION = 1

# How long the build job waits for the head to finish its fan-out before
# giving up on early release. The head writes the manifest immediately
# after its last `sbatch`, so in practice the file is there long before
# the first compile ends; this bound covers the head that never got that
# far, and keeps a build job from waiting on it for the length of a
# compile.
GATES_WAIT_S = 120.0
GATES_POLL_S = 2.0


def write_gates(path, *, run_token, entries) -> Path:
    """Write the head's plan-index → job-id manifest; return ``path``.

    ``entries`` is an iterable of ``(plan index, test name, job id,
    cluster)`` — one per submitted (test, run_id), so a plan index fanned
    out over several runs legitimately appears more than once. Job ids are
    the backend's own: ``"1234"`` for a single submission, ``"1234_3"`` for
    an array element.

    The cluster is recorded PER ENTRY, not once for the file. A job id is
    unique only within the cluster that issued it (#509), and
    ``--clusters=a,b`` places each array wherever it can start first — so
    one run's fan-out can legitimately span two clusters, and a single
    file-level value would aim half the releases at a stranger's job id.
    ``None`` means the local cluster and is omitted from the entry.

    ``run_token`` is the head's per-invocation nonce, carried so the build
    job can tell this run's manifest from one an earlier run left at the
    same path (the path is keyed on the head pid, which the OS reuses).

    Written tmp + replace like the plan: the build job polls for this file
    and must never read a half-written one.
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
    tmp.replace(path)  # atomic: the build job never reads a partial manifest
    return path


def load_gates(path) -> tuple[dict | None, str | None]:
    """``(payload, why not)`` — read the manifest without ever raising.

    A build job that raised here would exit non-zero and take the whole
    ``afterok`` fan-out with it (Slurm cancels dependants of a failed
    job), which is a spectacular price for an optimization that was not
    available. Every failure is therefore a reason string.
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
    """Poll for the manifest until it appears; ``(payload, why not)``.

    The build job is submitted BEFORE the sims it gates, so at its start
    this file does not exist — "absent" means "the head is still
    submitting", not "no early release". It only stops meaning that after
    ``timeout_s``, at which point the head is presumed gone and the run
    falls back to plain ``afterok``.

    A manifest carrying another run's ``run_token`` is rejected outright
    rather than waited on: it is a real file, it will not be replaced, and
    its job ids belong to jobs this build job has no business touching.
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

    One plan index can map to several ids: a test fanned out over N
    ``run_ids`` is one config in the plan and N rows in the fan-out. Order
    follows ``indices``, duplicates are dropped, and a malformed entry is
    skipped rather than failing the lookup — the manifest is advisory, and
    releasing the ids it *did* state correctly beats releasing none.

    Batched by cluster because that is what ``scontrol`` is addressed with:
    ids from different clusters cannot go in one call, and the caller must
    not have to discover that by having a release silently name a job on
    the wrong controller.
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
            # A manifest that cannot say where the id lives is not one to
            # guess for: fall back to the local cluster, the same as a
            # single-cluster site's absent field.
            cluster = None
        by_index.setdefault(index, []).append((job_id, cluster))
    batches: dict[str | None, list[str]] = {}
    # Keyed on the PAIR, because an id is only unique within its cluster:
    # `--clusters=a,b` can hand out 77_1 on each, and deduping on the
    # number alone would drop one of the two real jobs — leaving a
    # simulation gated on a build that had already released it (#548
    # review).
    seen: set[tuple[str | None, str]] = set()
    for index in indices:
        for job_id, cluster in by_index.get(index, ()):
            if (cluster, job_id) in seen:
                continue
            seen.add((cluster, job_id))
            batches.setdefault(cluster, []).append(job_id)
    return list(batches.items())
