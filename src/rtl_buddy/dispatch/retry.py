# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Classifying a missing result, and how long to wait before trying again (#405).

A dispatched job that leaves no result envelope is a failure by default,
and that default is not negotiable: a job that vanished must never score
green. But one shape of it is not a test failure at all — the simulation
was sitting in the VCS license queue, correctly waiting for a seat, when
the scheduler's reservation ran out underneath it and killed the whole
allocation. Re-running that job is the right answer; re-running a hung
testbench is not, and the two are indistinguishable from the scheduler
state alone (both die ``TIMEOUT`` at the reservation).

The discriminator survives the kill, in the job's own artefacts: a sim
that was *still queueing* when it died left output ending in the
``-licqueue`` banner and nothing else. The banner's mere presence is not
that discriminator — most jobs that print it go on to get a seat and run
(376 of 657 in the reported run) — so the rule is the same one
:class:`~rtl_buddy.tools.vcs_license.VcsLicenseQueueMonitor` applies live:
after the last marker, a complete line outside
:func:`~rtl_buddy.tools.vcs_license.is_queue_banner_line`'s vocabulary
means the seat was granted. A sim that queued, ran and then hung has such
a line and is not retried; a hung sim that never queued has no marker at
all and is not retried either.

:func:`classify_missing_result` layers that evidence on three gates:

* the attempt must have been **launchable** — the suite's build job, if
  there is one, has to have reported success. A sim whose build failed or
  crashed never started (``afterok`` cancelled it, or the pool skipped
  it), so its "evidence" cannot be from this attempt, and resubmitting it
  would run, ungated, a job the head deliberately skipped.
* the evidence must be **fresh**. ``artefacts/<test>/test.log`` is not
  cleaned between runs, so yesterday's banner would otherwise satisfy the
  rule forever; a candidate file older than this attempt's submission is
  ignored.
* on a scheduler-backed backend, the state must be a *resource* kill.

A backend that runs jobs itself has no scheduler state to read at all
(``DispatchBackend.scheduled`` is False, and ``collect_telemetry`` returns
nothing), so on that gate it would answer "no" for every job and retry
would be dead code there. For those backends the queue evidence carries
the decision alone: the head killed nothing, so a job that left no
envelope while its sim was demonstrably still waiting for a seat is the
same shape, minus a scheduler to name it.

:func:`backoff_delay` supplies the *when*. The jobs that lose a seat race
lose it together — they queued behind the same exhausted pool and their
reservations expire within seconds of each other — so an un-jittered retry
puts the whole batch back in front of a still-full pool in lockstep and
they time out together again. Growing plus randomised is what decorrelates
them; the delay itself is served by the backend
(``sbatch --begin=now+<delay>`` on Slurm, so the scheduler holds the job
and no allocation is burned while it waits; the pool's own gate on
local-parallel, which has no scheduler to hold anything).

Scope: **sim jobs only.** A build job's elaboration honours ``-licqueue``
exactly as the sim does (#358), so it can lose the same race — but a build
kill dooms a whole suite's fan-out at once and re-running it is a much
larger bet than re-running one sim, so it stays out until there is a
reported case for it.
"""

import os
import random

from ..config.dispatch import RETRY_CLASSIFIER_LICENSE_QUEUE
from ..tools.artifact_paths import test_artifact_dir
from ..tools.vcs_license import has_license_queue_marker, is_queue_banner_line
from .argv import job_log_path

# Scheduler states that mean "the job lost its allocation", as opposed to
# "the job ran and decided something". Only these are candidates: a
# FAILED/CANCELLED job made its own outcome, and re-running it would
# re-run a real failure.
RESOURCE_KILL_STATES = frozenset({"TIMEOUT", "NODE_FAIL", "PREEMPTED"})

# Cap on how much of one artefact is scanned for the banner. The banner is
# printed where the sim starts, so it is never deep in a multi-gigabyte
# transcript, and collection must not read a run's whole output back off a
# shared filesystem to answer one yes/no question. Counted in *characters*
# (the read is decoded text), not bytes: this is a performance guard, and
# reading somewhat more of a multi-byte log than the nominal budget costs
# nothing worth the precision.
_MAX_SCAN_CHARS = 4 * 1024 * 1024
_CHUNK_CHARS = 256 * 1024

# How much older than this attempt's submission a candidate artefact may
# be and still count as this attempt's. Zero would be the literal rule,
# but the file is written by a compute node and the timestamp is compared
# against the head's clock, so a second or two of NTP skew (or a coarse
# mtime granularity on the share) must not silently disable retry. It is
# far shorter than any queue wait worth retrying, so it cannot resurrect a
# previous run's log.
_MTIME_SKEW_GRACE_SEC = 5.0

# What one artefact says about the license queue.
EVIDENCE_NONE = "none"  # no banner, or nothing readable/fresh to read
EVIDENCE_QUEUED = "queued"  # banner, and only banner vocabulary after it
EVIDENCE_RAN = "ran"  # banner, then real simulator output: it got a seat


def normalise_scheduler_state(state) -> str:
    """The bare state word from an sacct state string.

    sacct decorates some states with an actor (``CANCELLED by 1234``), and
    a couple of spellings carry a ``+`` suffix when truncated
    (``TIMEOUT+``). Compare on the word alone so neither shape silently
    falls out of :data:`RESOURCE_KILL_STATES`.
    """
    if not state:
        return ""
    return str(state).strip().split()[0].rstrip("+").upper()


def _is_fresh(path, submitted_at) -> bool:
    """Was this file written by the attempt that started at ``submitted_at``?

    ``artefacts/<test>/test.log`` and ``test.err`` are keyed on the test,
    not on the run, and nothing cleans them between runs — so without this
    a banner printed days ago would satisfy the retry rule forever, and a
    job that never started would be retried on a previous run's evidence.
    ``None`` (no submission time known) keeps the unguarded behaviour, for
    callers outside the collect path.
    """
    if submitted_at is None:
        return True
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return False
    return mtime >= submitted_at - _MTIME_SKEW_GRACE_SEC


def file_queue_evidence(path, *, submitted_at=None) -> str:
    """What this one artefact says about the license queue (bounded read).

    Returns :data:`EVIDENCE_QUEUED` only when the file's output *ends* in
    the queue banner: after the last marker, every complete line is
    :func:`~rtl_buddy.tools.vcs_license.is_queue_banner_line` vocabulary.
    A complete line outside it means simv was granted its seat and ran —
    :data:`EVIDENCE_RAN`, which is the common shape (most jobs that print
    the banner do get a seat) and must never be retried as if the sim had
    never left the queue.

    A *trailing partial* line does not end the queued state, matching
    :class:`~rtl_buddy.tools.vcs_license.VcsLicenseQueueMonitor`: VCS
    appends its polling dots with no newline, and a killed job's last line
    is truncated anyway, so only a complete line is allowed to decide.

    Read forward in chunks, stopping as soon as the answer is known or at
    :data:`_MAX_SCAN_CHARS` — collection must not read a run's whole
    output back off a shared filesystem to answer one yes/no question.
    Hitting the cap with the question still open answers
    :data:`EVIDENCE_NONE`: unknown is not evidence, and the caller's
    default is not to retry.
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
                    # EOF. A partial last line only ever *enters* the queued
                    # state (the dots VCS never terminates), never leaves it.
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
        # An artefact that cannot be read is not evidence of queueing, and
        # the caller's default (no retry, count the failure) is the safe
        # reading of "no evidence".
        return EVIDENCE_NONE
    return EVIDENCE_NONE


def job_output_paths(spec) -> list:
    """Everything one sim job captured, in the order worth searching.

    The sim's own ``test.log``/``test.err`` first — that is where ``simv``
    prints the banner — then the job's rtl_buddy log and the scheduler's
    stdout log beside it (#437), which catch the case where the sim's
    output never reached its artefact files.
    """
    paths = []
    run_id = getattr(spec, "run_id", None)
    suite_dir = getattr(spec, "suite_dir", None)
    test_name = getattr(spec, "test_name", None)
    if suite_dir is not None and test_name is not None:
        artefacts = test_artifact_dir(suite_dir, test_name, run_id=run_id)
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
    """Why this job left no result, if it is a reason worth retrying.

    Returns the classifier name (today only ``"license-queue"``) or
    ``None`` — and ``None`` is the answer for everything the rule does not
    positively recognise, including an unknown scheduler state, stale or
    unreadable artefacts, and a job whose output shows it got its seat.

    ``scheduled`` is the submitting backend's
    :attr:`~rtl_buddy.dispatch.base.DispatchBackend.scheduled`. When True
    (Slurm) a *resource* scheduler state is required, so a job that FAILED
    or was CANCELLED on its own merits is never retried and a backend that
    reports no state at all retries nothing. When False (the local pool)
    there is no scheduler state to require — no accounting source exists —
    so the queue evidence carries the decision alone; demanding a state
    there would make the rule unsatisfiable and retry silently dead on
    that backend.

    ``build_succeeded`` is False when the suite had a build job that did
    not report success. Then this job never ran: ``afterok`` cancelled it,
    or the pool skipped it because its build failed. Nothing it could be
    holding is this attempt's evidence, and resubmitting it would launch —
    ungated — a sim the head deliberately skipped, so it is not a retry
    candidate whatever its artefacts say.

    ``submitted_at`` is when this attempt was submitted; artefacts older
    than that are a previous run's and are ignored (see :func:`_is_fresh`).
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
    # One capture showing the sim ran outranks another that only shows it
    # queued: the banner and the output that followed it can land in
    # different files (stdout vs the scheduler's log), and "it got a seat"
    # is the fact that settles the question.
    if EVIDENCE_RAN in evidence:
        return None
    if EVIDENCE_QUEUED in evidence:
        return RETRY_CLASSIFIER_LICENSE_QUEUE
    return None


def backoff_delay(attempt: int, retry_cfg, *, rng=None) -> float:
    """Seconds to hold attempt ``attempt`` (1 = the first retry).

    ``min(backoff-max-sec, backoff-sec * 2 ** (attempt - 1))``, then
    scaled by ``uniform(1 - jitter, 1 + jitter)``. The cap is applied
    *before* the jitter on purpose: it bounds the schedule being spread,
    not the spread itself, so a capped delay still lands somewhere in a
    window instead of every retry in the batch landing on the cap.
    """
    rng = rng if rng is not None else random
    base = min(
        retry_cfg.backoff_max_sec,
        retry_cfg.backoff_sec * (2 ** max(0, attempt - 1)),
    )
    if retry_cfg.jitter:
        base *= rng.uniform(1 - retry_cfg.jitter, 1 + retry_cfg.jitter)
    return max(0.0, base)
