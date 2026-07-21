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

Like pytest xfail without ``raises=``, this does not distinguish a
genuine check failure from an infrastructure error that also surfaces as
``FAIL``. Reserve xfail for checks whose failure is understood.

Result classes opt in by delegating their ``is_pass()`` to
:func:`is_pass_with_xfail`; the command remaps a result with
:func:`apply_xfail` right after the runner returns, when the config in
scope reports ``is_xfail()``.
"""

# Statuses that always count as a pass. XPASS is handled separately
# because whether it passes depends on the recorded strictness.
_BASE_PASS = ("PASS", "SKIP", "XFAIL")


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


def apply_xfail(result, *, strict: bool = False):
    """Re-interpret a result record in place under an xfail marker.

    ``result`` is any ``*Results`` object exposing a mutable ``.results``
    dict whose ``is_pass()`` delegates to :func:`is_pass_with_xfail`.
    ``FAIL`` becomes ``XFAIL`` (a pass); ``PASS`` becomes ``XPASS`` (a
    pass when non-strict, a failure when ``strict``); ``SKIP`` / ``NA``
    are left untouched. Returns ``result`` for convenience.
    """
    status = result.results.get("result")
    if status == "FAIL":
        result.results["result"] = "XFAIL"
        result.results["desc"] = "xfail (expected fail): " + str(
            result.results.get("desc", "")
        )
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
