"""Expected-fail (xfail) handling shared by the result records of test, fpv, synth, cdc, pnr and power.

A check is expected to fail when its config sets ``xfail`` or ``xfail_strict``:

- ``FAIL`` becomes ``XFAIL``, which counts as a pass.
- ``PASS`` becomes ``XPASS``, which counts as a pass for a non-strict xfail and a failure for a strict one.
- ``SKIP`` and ``NA`` are unchanged.

The marker excuses only a failure that the tool reported as its verdict. A failure that replaced the verdict
(preproc or compile failure, sim timeout, lost scheduler job) stays ``FAIL``; such a result carries :data:`FAIL_STAGE_KEY`, see :func:`xfail_refusal`.

Result classes delegate ``is_pass()`` to :func:`is_pass_with_xfail`. The command calls :func:`apply_xfail` after the runner returns when the config reports ``is_xfail()``.
"""

# XPASS is handled separately: it passes only when non-strict.
_BASE_PASS = ("PASS", "SKIP", "XFAIL")

# Set in a FAIL results dict to the stage that failed instead of the flow producing its own verdict.
# It lives in the dict, not the class, because a dispatched job's envelope carries only the dict back to the head.
# A dict without the key counts as the flow's own verdict.
FAIL_STAGE_KEY = "fail_stage"

# Stage names are part of the machine-output contract; the phrases appear in summary rows.
FAIL_STAGE_REASONS = {
    "setup": "setup failure",
    "compile": "compile failure",
    "sim_timeout": "sim timeout",
    "sim": "sim ended without a verdict",
    "dispatch": "dispatch failure",
    "tool": "tool failure before a verdict",
    "export": "requested export not delivered",
    "abstract": "hardened abstract not produced",
}


def is_pass_with_xfail(results: dict) -> bool:
    """``is_pass()`` body shared by all xfail-aware result classes.

    ``PASS``, ``SKIP`` and ``XFAIL`` pass; ``XPASS`` passes unless ``results["xfail_strict"]`` is set; anything else fails.
    """
    result = results.get("result")
    if result in _BASE_PASS:
        return True
    if result == "XPASS":
        return not results.get("xfail_strict", False)
    return False


def xfail_refusal(results: dict) -> str | None:
    """Return the reason an xfail marker must not excuse this result, or ``None``.

    A dict carrying :data:`FAIL_STAGE_KEY` failed before or instead of a verdict, so the marker does not apply. An unknown stage value is returned verbatim.
    """
    stage = results.get(FAIL_STAGE_KEY)
    if not stage:
        return None
    return FAIL_STAGE_REASONS.get(stage, str(stage))


def apply_xfail(result, *, strict: bool = False):
    """Re-interpret a result record in place under an xfail marker and return it.

    ``result`` is any ``*Results`` object with a mutable ``.results`` dict whose ``is_pass()`` delegates to :func:`is_pass_with_xfail`.
    ``FAIL`` becomes ``XFAIL`` unless :func:`xfail_refusal` gives a reason, in which case the ``FAIL`` stands and its desc says why.
    ``PASS`` becomes ``XPASS``, a failure when ``strict``. ``SKIP`` and ``NA`` are unchanged.
    """
    status = result.results.get("result")
    if status == "FAIL":
        refusal = xfail_refusal(result.results)
        if refusal is None:
            result.results["result"] = "XFAIL"
            note = "xfail (expected fail): "
        else:
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
