"""Unit tests for the shared xfail helper (runner/xfail.py), the single
source of truth for expected-fail re-interpretation used by every
command's result classes."""

import pytest

from rtl_buddy.runner.xfail import apply_xfail, is_pass_with_xfail


class _Result:
    """Minimal stand-in for a *Results object: just a mutable dict."""

    def __init__(self, status, desc="d", **extra):
        self.results = {"result": status, "desc": desc, **extra}


# ---------------------------------------------------------------------------
# is_pass_with_xfail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["PASS", "SKIP", "XFAIL"])
def test_is_pass_true_statuses(status):
    assert is_pass_with_xfail({"result": status}) is True


@pytest.mark.parametrize("status", ["FAIL", "NA", "WHATEVER"])
def test_is_pass_false_statuses(status):
    assert is_pass_with_xfail({"result": status}) is False


def test_is_pass_xpass_nonstrict_passes():
    assert is_pass_with_xfail({"result": "XPASS", "xfail_strict": False}) is True
    # absent flag behaves as non-strict
    assert is_pass_with_xfail({"result": "XPASS"}) is True


def test_is_pass_xpass_strict_fails():
    assert is_pass_with_xfail({"result": "XPASS", "xfail_strict": True}) is False


# ---------------------------------------------------------------------------
# apply_xfail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strict", [False, True])
def test_apply_xfail_fail_to_xfail_passes_either_strictness(strict):
    res = _Result("FAIL", desc="boom")
    apply_xfail(res, strict=strict)
    assert res.results["result"] == "XFAIL"
    assert is_pass_with_xfail(res.results) is True
    assert res.results["desc"].startswith("xfail (expected fail): ")
    assert res.results["desc"].endswith("boom")


def test_apply_xfail_pass_nonstrict_to_xpass_still_passes():
    res = _Result("PASS")
    apply_xfail(res, strict=False)
    assert res.results["result"] == "XPASS"
    assert res.results["xfail_strict"] is False
    assert is_pass_with_xfail(res.results) is True
    assert res.results["desc"].startswith("XPASS (expected fail but passed): ")


def test_apply_xfail_pass_strict_to_xpass_fails():
    res = _Result("PASS")
    apply_xfail(res, strict=True)
    assert res.results["result"] == "XPASS"
    assert res.results["xfail_strict"] is True
    assert is_pass_with_xfail(res.results) is False
    assert res.results["desc"].startswith(
        "XPASS (expected fail but passed — strict, failing): "
    )


@pytest.mark.parametrize("status", ["SKIP", "NA"])
def test_apply_xfail_skip_and_na_pass_through(status):
    res = _Result(status, desc="kept")
    apply_xfail(res, strict=True)
    assert res.results["result"] == status
    assert res.results["desc"] == "kept"


def test_apply_xfail_default_strict_is_false():
    res = _Result("PASS")
    apply_xfail(res)  # strict defaults to False
    assert is_pass_with_xfail(res.results) is True
