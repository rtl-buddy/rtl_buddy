# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""
vlog_post module handles post-processing of output from verilog simulations for rtl-buddy
"""

import logging
import os

logger = logging.getLogger(__name__)
import re
from ..runner.test_results import TestResults
from ..logging_utils import log_event


# Verilator emits SVA failures as a `%Error` line; the same shape covers
# immediate and concurrent assertions. Cover hits are not surfaced as errors.
# Example: `%Error: dut.sv:42: Assertion failed in top.dut: 'signal == expected'`
_ASSERTION_FAILED_RE = re.compile(
    r"^%Error[^:]*:\s*[^:]+:\s*\d+:\s*Assertion failed",
)


def count_assertion_failures(*paths) -> int:
    """Count Verilator-style `%Error: <file>:<line>: Assertion failed` lines.

    Reads the listed log/err files; missing files are skipped. Used by
    `VlogPost` to surface assertion firings in the `rb test` results table
    when `tests.yaml` enables `assertions: true`.
    """
    total = 0
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", errors="replace") as f:
                for line in f:
                    if _ASSERTION_FAILED_RE.match(line):
                        total += 1
        except OSError:
            continue
    return total


class VlogPost:
    """
    Verilog test output post-processing
    """

    def __init__(self, name, path, *, err_path=None, assertions_enabled=False):
        self.name = name
        self.path = path
        self.err_path = err_path
        self.assertions_enabled = assertions_enabled

    def get_results(self):
        """
        return default TestResults
        """
        match_pass = None
        match_fail = None
        match_err = None
        with open(self.path, "r") as f:
            for line in f.readlines():
                if match_pass is None:
                    match_pass = re.search(r"^PASS\s*(.*)", line)
                if match_fail is None:
                    match_fail = re.search(r"^FAIL\s*(.*)", line)
                if match_err is None:
                    match_err = re.search(r"^(ERR|FAT):\s*(.*)", line)

        results = {"result": "NA", "desc": "test result unknown"}
        if match_fail is not None:
            results = {
                "result": "FAIL",
                "desc": f"{match_fail.group(1)} {match_err.group(2).strip()}",
            }
        if match_pass is not None:
            results = {"result": "PASS", "desc": match_pass.group(1)}
        if match_pass is None and match_fail is None:
            log_event(
                logger,
                logging.WARNING,
                "postproc.no_markers",
                test=self.name,
                log=str(self.path),
            )

        self._merge_assertions(results)
        return TestResults(name=self.name, results=results)

    def _merge_assertions(self, results: dict) -> None:
        """Annotate `results` with the SVA assertion count if enabled.

        An assertion failure is itself a test failure: Verilator aborts on
        `%Error: Assertion failed`, but if the testbench wrapper swallowed the
        abort (or printed PASS earlier in the same log) we still want to flag
        the firing here so the results table tells the truth.
        """
        if not self.assertions_enabled:
            return
        fired = count_assertion_failures(self.path, self.err_path)
        results["assertions"] = {"enabled": True, "fired": fired}
        if fired > 0 and results.get("result") != "FAIL":
            prev_result = results.get("result", "NA")
            prev_desc = results.get("desc", "")
            results["result"] = "FAIL"
            results["desc"] = (
                f"{fired} SVA assertion failure(s) (was {prev_result}: {prev_desc})"
            )


class UvmVlogPost(VlogPost):
    """
    UVM report post-processing
    """

    def __init__(
        self,
        name,
        path,
        max_warns,
        max_errors,
        *,
        err_path=None,
        assertions_enabled=False,
    ):
        super().__init__(
            name=name,
            path=path,
            err_path=err_path,
            assertions_enabled=assertions_enabled,
        )
        self.max_warns = max_warns
        self.max_errors = max_errors

    def get_results(self):
        """
        return UVM TestResults
        """

        results = {}
        with open(self.path, "r") as f:
            summary = re.search(
                r"-+\s*UVM Report Summary\s*-+\s*\**\s*Report counts by severity\s*((?:UVM_(?:INFO|WARNING|ERROR|FATAL)\s*:?\s*[0-9]+\s?)+)",
                f.read(),
            )

            if summary is None:
                results = {
                    "result": "FAIL",
                    "desc": f"No UVM Report Summary detected. See {self.path}.",
                }
            else:
                totals = dict(
                    map(
                        lambda match: (match.group(1), int(match.group(2))),
                        re.finditer(
                            r"^UVM_(INFO|WARNING|ERROR|FATAL)\s*:?\s*([0-9]+)",
                            summary.group(1),
                            re.MULTILINE,
                        ),
                    )
                )
                if (
                    "WARNING" not in totals
                    or "ERROR" not in totals
                    or "FATAL" not in totals
                ):
                    results = {
                        "result": "FAIL",
                        "desc": f"Invalid UVM Report Summary detected. See {self.path}",
                    }
                else:
                    message_summary = ", ".join(
                        map(
                            lambda kv: (
                                f"{kv[1]} uvm {kv[0].lower()}{'s' if kv[1] != 1 else ''}"
                            ),
                            filter(lambda kv: kv[0] != "INFO", totals.items()),
                        )
                    )
                    results_str = f"{message_summary} detected. max_warnings={self.max_warns}, max_err={self.max_errors}"
                    if (
                        totals["WARNING"] <= self.max_warns
                        and totals["ERROR"] <= self.max_errors
                        and totals["FATAL"] <= 0
                    ):
                        results = {"result": "PASS", "desc": results_str}
                    else:
                        results = {
                            "result": "FAIL",
                            "desc": f"{results_str}. See {self.path}",
                        }

        self._merge_assertions(results)
        return TestResults(name=self.name, results=results)
