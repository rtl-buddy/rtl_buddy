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
that queued printed the ``-licqueue`` banner. :func:`classify_missing_result`
is therefore a two-part rule — a *resource* scheduler state AND the banner
in what the job captured — using the same
:func:`~rtl_buddy.tools.vcs_license.has_license_queue_marker` the compile
phase already applies to its own output (#358). A hung test reaches its
reservation with no banner and is not retried.

A backend that runs jobs itself has no scheduler state to read at all
(``DispatchBackend.scheduled`` is False, and ``collect_telemetry`` returns
nothing), so on that half of the rule it would answer "no" for every job
and retry would be dead code there. For those backends the banner alone
decides: the head killed nothing, so a job that left no envelope while its
sim was demonstrably waiting for a seat is the same shape, minus a
scheduler to name it.

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

import random

from ..config.dispatch import RETRY_CLASSIFIER_LICENSE_QUEUE
from ..tools.artifact_paths import test_artifact_dir
from ..tools.vcs_license import has_license_queue_marker
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


def _file_has_marker(path) -> bool:
    """Does this file contain the license-queue banner (bounded read)?

    Read in chunks with a one-marker overlap so a banner straddling a
    chunk boundary is still found, and stop at :data:`_MAX_SCAN_CHARS`.
    """
    if path is None:
        return False
    overlap = 64  # longer than the longest marker string
    try:
        with open(path, "r", errors="replace") as fh:
            read = 0
            tail = ""
            while read < _MAX_SCAN_CHARS:
                chunk = fh.read(_CHUNK_CHARS)
                if not chunk:
                    return False
                read += len(chunk)
                if has_license_queue_marker(tail + chunk):
                    return True
                tail = chunk[-overlap:]
    except OSError:
        # An artefact that cannot be read is not evidence of queueing, and
        # the caller's default (no retry, count the failure) is the safe
        # reading of "no evidence".
        return False
    return False


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
    spec, scheduler_state, *, classifiers, scheduled: bool = True
) -> str | None:
    """Why this job left no result, if it is a reason worth retrying.

    Returns the classifier name (today only ``"license-queue"``) or
    ``None`` — and ``None`` is the answer for everything the rule does not
    positively recognise, including an unknown scheduler state and a job
    whose artefacts show no queue banner.

    ``scheduled`` is the submitting backend's
    :attr:`~rtl_buddy.dispatch.base.DispatchBackend.scheduled`. When True
    (Slurm) a *resource* scheduler state is required, so a job that FAILED
    or was CANCELLED on its own merits is never retried and a backend that
    reports no state at all retries nothing. When False (the local pool)
    there is no scheduler state to require — no accounting source exists —
    so the banner carries the decision alone; demanding a state there would
    make the rule unsatisfiable and retry silently dead on that backend.
    """
    if RETRY_CLASSIFIER_LICENSE_QUEUE not in (classifiers or ()):
        return None
    if scheduled:
        if normalise_scheduler_state(scheduler_state) not in RESOURCE_KILL_STATES:
            return None
    if any(_file_has_marker(path) for path in job_output_paths(spec)):
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
