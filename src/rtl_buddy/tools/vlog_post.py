# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""
vlog_post module handles post-processing of output from verilog simulations for rtl-buddy
"""

import logging

logger = logging.getLogger(__name__)
import re
from ..runner.test_results import TestResults


class VlogPost:
    """
    Verilog test output post-processing
    """

    def __init__(self, name, path):
        self.name = name
        self.path = path

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

        return TestResults(name=self.name, results=results)


class UvmVlogPost(VlogPost):
    """
    UVM report post-processing
    """

    def __init__(self, name, path, max_warns, max_errors):
        super().__init__(name=name, path=path)
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

        return TestResults(name=self.name, results=results)
