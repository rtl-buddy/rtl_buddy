# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Decides whether a job that left no result should be retried, and how long to wait.

A missing result is a failure unless the sim was still waiting in the VCS
license queue when its reservation expired. :func:`classify_missing_result`
recognises that case from the job's output; :func:`backoff_delay` gives the
jittered delay before the retry. Only sim jobs are retried, not build jobs.
"""

import os
import random

from ..config.dispatch import RETRY_CLASSIFIER_LICENSE_QUEUE
from ..tools.artifact_paths import test_artifact_dir
from ..tools.vcs_license import has_license_queue_marker, is_queue_banner_line
from .argv import job_log_path

# States meaning the job lost its allocation. A FAILED or CANCELLED job made its
# own outcome and is not retried.
RESOURCE_KILL_STATES = frozenset({"TIMEOUT", "NODE_FAIL", "PREEMPTED"})

# Read cap, in characters, so collection does not read a whole transcript off a
# shared filesystem. The banner appears where the sim starts.
_MAX_SCAN_CHARS = 4 * 1024 * 1024
_CHUNK_CHARS = 256 * 1024

# Tolerance for clock skew between the compute node's mtime and the head's clock.
_MTIME_SKEW_GRACE_SEC = 5.0

# What one artefact says about the license queue.
EVIDENCE_NONE = "none"  # no banner, or nothing readable/fresh to read
EVIDENCE_QUEUED = "queued"  # banner, and only banner vocabulary after it
EVIDENCE_RAN = "ran"  # banner, then real simulator output: it got a seat


def normalise_scheduler_state(state) -> str:
    """The bare state word from an sacct state string.

    Strips actors (``CANCELLED by 1234``) and the truncation ``+`` (``TIMEOUT+``).
    """
    if not state:
        return ""
    return str(state).strip().split()[0].rstrip("+").upper()


def _is_fresh(path, submitted_at) -> bool:
    """Was this file written by the attempt that started at ``submitted_at``?

    A job removes its run's ``test.log`` and ``test.err`` before it starts, but one
    that never started leaves the previous run's, so older files are ignored.
    ``None`` means no submission time is known and accepts the file.
    """
    if submitted_at is None:
        return True
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return False
    return mtime >= submitted_at - _MTIME_SKEW_GRACE_SEC


def file_queue_evidence(path, *, submitted_at=None) -> str:
    """What one artefact says about the license queue (bounded read).

    :data:`EVIDENCE_QUEUED` means that after the last banner marker every
    complete line is queue-banner vocabulary. :data:`EVIDENCE_RAN` means a
    complete line outside it followed, so the sim got its seat. A trailing
    partial line does not end the queued state. Reading stops at
    :data:`_MAX_SCAN_CHARS`; an unresolved scan is :data:`EVIDENCE_NONE`.
    """
    if path is None or not _is_fresh(path, submitted_at):
        return EVIDENCE_NONE
    queued = False
    try:
        with open(path, "r", errors="replace") as fh:
            read = 0
            buffer = ""
            while read < _MAX_SCAN_CHARS:
                chunk = fh.read(_CHUNK_CHARS)
                if not chunk:
                    # A partial last line can enter the queued state, never leave it.
                    if not queued and has_license_queue_marker(buffer):
                        queued = True
                    return EVIDENCE_QUEUED if queued else EVIDENCE_NONE
                read += len(chunk)
                lines = (buffer + chunk).split("\n")
                buffer = lines.pop()
                for line in lines:
                    if has_license_queue_marker(line):
                        queued = True
                    elif queued and not is_queue_banner_line(line):
                        return EVIDENCE_RAN
    except OSError:
        return EVIDENCE_NONE
    return EVIDENCE_NONE


def job_output_paths(spec) -> list:
    """Files that may hold the sim's license banner, in search order.

    The sim's ``test.log`` and ``test.err``, then the job's rtl_buddy log and the
    scheduler's stdout log.
    """
    paths = []
    run_id = getattr(spec, "run_id", None)
    suite_dir = getattr(spec, "suite_dir", None)
    test_name = getattr(spec, "test_name", None)
    if suite_dir is not None and test_name is not None:
        artefacts = test_artifact_dir(
            suite_dir,
            test_name,
            run_id=run_id,
            run_tag=getattr(spec, "run_tag", None),
        )
        paths += [artefacts / "test.log", artefacts / "test.err"]
    result_json = getattr(spec, "result_json", None)
    if result_json is not None:
        paths.append(job_log_path(result_json))
    log_path = getattr(spec, "log_path", None)
    if log_path is not None:
        paths.append(log_path)
    return paths


def classify_missing_result(
    spec,
    scheduler_state,
    *,
    classifiers,
    scheduled: bool = True,
    build_succeeded: bool = True,
    submitted_at=None,
) -> str | None:
    """Why this job left no result, if it is worth retrying.

    Returns the classifier name (``"license-queue"``) or ``None`` for anything
    not positively recognised.

    ``scheduled`` is the backend's ``DispatchBackend.scheduled``. When True, a
    resource scheduler state is required; when False (local pool) there is no
    state, so queue evidence alone decides. ``build_succeeded`` False means the
    job never ran because its build failed, so it is never retried.
    ``submitted_at`` is the attempt's submission time; older artefacts are
    ignored (see :func:`_is_fresh`).
    """
    if RETRY_CLASSIFIER_LICENSE_QUEUE not in (classifiers or ()):
        return None
    if not build_succeeded:
        return None
    if scheduled:
        if normalise_scheduler_state(scheduler_state) not in RESOURCE_KILL_STATES:
            return None
    evidence = [
        file_queue_evidence(path, submitted_at=submitted_at)
        for path in job_output_paths(spec)
    ]
    # The banner and later output can land in different files; "ran" outranks "queued".
    if EVIDENCE_RAN in evidence:
        return None
    if EVIDENCE_QUEUED in evidence:
        return RETRY_CLASSIFIER_LICENSE_QUEUE
    return None


def backoff_delay(attempt: int, retry_cfg, *, rng=None) -> float:
    """Seconds to hold attempt ``attempt`` (1 = the first retry).

    ``min(backoff-max-sec, backoff-sec * 2 ** (attempt - 1))`` scaled by
    ``uniform(1 - jitter, 1 + jitter)``. The cap applies before jitter so
    capped retries still spread out.
    """
    rng = rng if rng is not None else random
    base = min(
        retry_cfg.backoff_max_sec,
        retry_cfg.backoff_sec * (2 ** max(0, attempt - 1)),
    )
    if retry_cfg.jitter:
        base *= rng.uniform(1 - retry_cfg.jitter, 1 + retry_cfg.jitter)
    return max(0.0, base)
