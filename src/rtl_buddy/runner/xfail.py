"""Shared expected-fail (xfail) handling for result records.

Used by every command whose result records carry a top-level ``result``
of ``PASS`` / ``FAIL`` / ``SKIP`` (test, fpv, synth, cdc, pnr, power). A
check is treated as expected-to-fail when its config sets ``xfail`` or
``xfail_strict``:

- ``FAIL`` -> ``XFAIL`` — the expected failure happened; counts as a pass.
- ``PASS`` -> ``XPASS`` — an unexpected pass. Counts as a pass for a
  non-strict xfail, or a FAILURE for a strict one (so a stale marker is
  loud).
- ``SKIP`` / ``NA`` -> unchanged.

The marker excuses only a failure the flow's own tool reported *as its
verdict*. A failure that happened instead of a verdict — a preproc or
compile failure, a sim killed at the timeout, a job the scheduler lost —
stays ``FAIL`` however the marker is spelled (#553, #594): a negative
control that stopped compiling, or that never reached the mismatch it
exists to catch, is not still catching it, and grading it green is a
silent false pass across a suite of them. Such a result says so itself,
by carrying :data:`FAIL_STAGE_KEY`; see :func:`xfail_refusal`.

Result classes opt in by delegating their ``is_pass()`` to
:func:`is_pass_with_xfail`; the command remaps a result with
:func:`apply_xfail` right after the runner returns, when the config in
scope reports ``is_xfail()``.
"""

# Statuses that always count as a pass. XPASS is handled separately
# because whether it passes depends on the recorded strictness.
_BASE_PASS = ("PASS", "SKIP", "XFAIL")

# Key a FAIL results dict carries when the failure is *not* the flow's own
# verdict on the thing it checks, naming the stage that failed instead. A
# structural signal rather than a match on the description text, so a
# reworded desc cannot quietly re-excuse a timeout — and it lives in the
# results dict, not the class, because that dict is all a dispatched job's
# envelope carries back to the collecting head (``from_json_dict`` rebuilds
# every kind as a plain ``TestResults``).
#
# Additive: the envelope's ``schema_version`` is unchanged and
# ``to_json_dict`` carries the key through for free. A results dict written
# by an older rtl_buddy has no key and reads as "the flow's own verdict",
# which is exactly how that result was already graded.
FAIL_STAGE_KEY = "fail_stage"

# The stages, and how a refused marker reports each one. Values are part of
# the machine-output contract; the phrases are what a summary row shows.
FAIL_STAGE_REASONS = {
    "setup": "setup failure",
    "compile": "compile failure",
    "sim_timeout": "sim timeout",
    "sim": "sim ended without a verdict",
    "dispatch": "dispatch failure",
    "tool": "tool failure before a verdict",
}


def is_pass_with_xfail(results: dict) -> bool:
    """``is_pass()`` body shared by all xfail-aware result classes.

    ``PASS`` / ``SKIP`` / ``XFAIL`` pass; ``XPASS`` passes only for a
    non-strict xfail (``results["xfail_strict"]`` falsy); anything else
    (``FAIL`` / ``NA`` / unknown) fails.
    """
    result = results.get("result")
    if result in _BASE_PASS:
        return True
    if result == "XPASS":
        return not results.get("xfail_strict", False)
    return False


def xfail_refusal(results: dict) -> str | None:
    """Why an xfail marker must not excuse this result, or ``None``.

    A results dict carrying :data:`FAIL_STAGE_KEY` failed before — or
    instead of — the verdict the marker is about, so the marker does not
    apply to it; the phrase returned is what the refusal is reported
    with. An unknown stage value reports itself verbatim rather than
    being silently excused.
    """
    stage = results.get(FAIL_STAGE_KEY)
    if not stage:
        return None
    return FAIL_STAGE_REASONS.get(stage, str(stage))


def apply_xfail(result, *, strict: bool = False):
    """Re-interpret a result record in place under an xfail marker.

    ``result`` is any ``*Results`` object exposing a mutable ``.results``
    dict whose ``is_pass()`` delegates to :func:`is_pass_with_xfail`.
    ``FAIL`` becomes ``XFAIL`` (a pass) *unless* :func:`xfail_refusal`
    names a reason it may not, in which case the ``FAIL`` stands and says
    why; ``PASS`` becomes ``XPASS`` (a pass when non-strict, a failure
    when ``strict``); ``SKIP`` / ``NA`` are left untouched. Returns
    ``result`` for convenience.
    """
    status = result.results.get("result")
    if status == "FAIL":
        refusal = xfail_refusal(result.results)
        if refusal is None:
            result.results["result"] = "XFAIL"
            note = "xfail (expected fail): "
        else:
            # The FAIL stands. The desc leads with the stage the marker
            # does not cover, because a summary table row is where a CI
            # reader meets this result first.
            note = f"xfail not applied ({refusal}): "
        result.results["desc"] = note + str(result.results.get("desc", ""))
    elif status == "PASS":
        result.results["result"] = "XPASS"
        result.results["xfail_strict"] = strict
        note = (
            "XPASS (expected fail but passed — strict, failing): "
            if strict
            else "XPASS (expected fail but passed): "
        )
        result.results["desc"] = note + str(result.results.get("desc", ""))
    return result
