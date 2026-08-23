"""Tests for the PASS/FAIL/ERR marker contract in `VlogPost.get_results`."""

import logging

from rtl_buddy.tools import vlog_post


def _results(tmp_path, text):
    log = tmp_path / "test.log"
    log.write_text(text)
    return vlog_post.VlogPost(name="t", path=str(log)).get_results().results


def test_fail_without_err_marker_reports_fail(tmp_path):
    # An ERR:/FAT: line is conventional alongside FAIL but not guaranteed --
    # a testbench may print its verdict and nothing else. Reading the ERR
    # match unconditionally raised AttributeError here, which aborted the
    # whole run instead of reporting the failure, taking the results table
    # for every other test in a regression with it.
    results = _results(tmp_path, "running...\nFAIL (nerr=1) the thing broke\n")
    assert results["result"] == "FAIL"
    assert results["desc"] == "(nerr=1) the thing broke"


def test_fail_without_err_marker_desc_has_no_trailing_space(tmp_path):
    results = _results(tmp_path, "FAIL\n")
    assert results["result"] == "FAIL"
    assert results["desc"] == ""


def test_fail_with_err_marker_appends_detail(tmp_path):
    results = _results(
        tmp_path,
        "ERR: scoreboard mismatch at 1200ns\nFAIL (nerr=1)\n",
    )
    assert results["result"] == "FAIL"
    assert results["desc"] == "(nerr=1) scoreboard mismatch at 1200ns"


def test_fail_with_fat_marker_appends_detail(tmp_path):
    results = _results(tmp_path, "FAT: bus model gave up\nFAIL (nerr=0, nfat=1)\n")
    assert results["result"] == "FAIL"
    assert results["desc"] == "(nerr=0, nfat=1) bus model gave up"


def test_pass_marker_reports_pass(tmp_path):
    results = _results(tmp_path, "ERR: earlier noise\nPASS (nwrn=2)\n")
    assert results["result"] == "PASS"
    assert results["desc"] == "(nwrn=2)"


def test_no_markers_reports_na(tmp_path):
    results = _results(tmp_path, "simulation finished\n")
    assert results["result"] == "NA"


def test_fail_wins_when_pass_appears_after_it(tmp_path):
    # A failure signal must not be erasable by a PASS line elsewhere in the
    # log: a per-phase PASS, a wrapper printing PASS after a failing
    # sub-check, or two phases' output concatenated would otherwise score a
    # failing run green.
    results = _results(tmp_path, "FAIL tb: 3 mismatches\nPASS tb: done\n")
    assert results["result"] == "FAIL"


def test_fail_wins_when_pass_appears_before_it(tmp_path):
    results = _results(tmp_path, "PASS tb: done\nFAIL tb: 3 mismatches\n")
    assert results["result"] == "FAIL"


def test_conflicting_markers_keep_the_fail_description(tmp_path):
    results = _results(
        tmp_path,
        "PASS tb: phase 1\nERR: dout mismatch\nFAIL tb: 1 mismatch\n",
    )
    assert results["result"] == "FAIL"
    assert results["desc"] == "tb: 1 mismatch dout mismatch"


def test_conflicting_markers_warn(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="rtl_buddy.tools.vlog_post"):
        results = _results(tmp_path, "PASS tb: done\nFAIL tb: 1 mismatch\n")
    assert results["result"] == "FAIL"
    assert "conflicting_markers" in caplog.text


def test_pass_alone_does_not_warn_about_conflict(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="rtl_buddy.tools.vlog_post"):
        results = _results(tmp_path, "PASS tb: done\n")
    assert results["result"] == "PASS"
    assert "conflicting_markers" not in caplog.text
