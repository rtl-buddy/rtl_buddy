"""Tests for the PASS/FAIL/ERR marker contract in `VlogPost.get_results`."""

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
