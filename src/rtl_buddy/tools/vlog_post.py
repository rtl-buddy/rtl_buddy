# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Grades Verilog simulation output from its PASS/FAIL markers, UVM summary and assertion failures."""

import logging
import os

logger = logging.getLogger(__name__)
import re
from ..runner.test_results import TestResults
from ..runner.xfail import FAIL_STAGE_KEY
from ..logging_utils import log_event


# Matches immediate and concurrent SVA failures, e.g.
# `%Error: dut.sv:42: Assertion failed in top.dut: 'signal == expected'`.
# `--timing` adds a leading `[<time>] `, so it is optional.
_ASSERTION_FAILED_RE = re.compile(
    r"^(?:\[\d+\]\s+)?%Error[^:]*:\s*[^:]+:\s*\d+:\s*Assertion failed",
)


def count_assertion_failures(*paths) -> int:
    """Count Verilator `%Error: <file>:<line>: Assertion failed` lines in the given files, skipping missing ones."""
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


def describe_sim_exit(sim_returncode) -> str:
    """Describe how a simulator ended: ``exited 1`` or, for a negative return code, ``killed by signal 6``."""
    if sim_returncode is not None and sim_returncode < 0:
        return f"killed by signal {-sim_returncode}"
    return f"exited {sim_returncode}"


def grade_unknown_sim_exit(results: dict, sim_returncode, *, test, run_id=None):
    """Re-grade a ``NA`` result to ``FAIL`` when the simulator exited nonzero.

    Mutates ``results`` in place and returns whether it changed. Results that
    state a verdict keep it, and a ``sim_returncode`` of ``None`` (no
    simulation ran) grades nothing. Call before ``postproc.completed`` is
    logged, since that event reports the final result.
    """
    if not sim_returncode or results.get("result") != "NA":
        return False
    log_event(
        logger,
        logging.ERROR,
        "sim.unknown_verdict",
        test=test,
        run_id=run_id,
        returncode=sim_returncode,
    )
    results["result"] = "FAIL"
    results["desc"] = (
        f"Sim {describe_sim_exit(sim_returncode)} with no PASS/FAIL "
        "verdict in the transcript"
    )
    # The simulator died without a verdict, so an xfail marker has nothing to excuse.
    results[FAIL_STAGE_KEY] = "sim"
    return True


class VlogPost:
    """Grades a test from PASS/FAIL markers in the sim log."""

    def __init__(self, name, path, *, err_path=None, assertions_enabled=False):
        self.name = name
        self.path = path
        self.err_path = err_path
        self.assertions_enabled = assertions_enabled

    def get_results(self):
        """Return TestResults graded from the log's PASS, FAIL and ERR/FAT lines."""
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
        if match_pass is not None:
            results = {"result": "PASS", "desc": match_pass.group(1)}
        # FAIL is applied last so a PASS line elsewhere in the log cannot mask it.
        if match_fail is not None:
            # An ERR:/FAT: line may be absent, so match_err can be None.
            detail = match_err.group(2).strip() if match_err is not None else ""
            desc = f"{match_fail.group(1)} {detail}".strip()
            results = {"result": "FAIL", "desc": desc}
            if match_pass is not None:
                log_event(
                    logger,
                    logging.WARNING,
                    "postproc.conflicting_markers",
                    test=self.name,
                    log=str(self.path),
                    chosen="FAIL",
                )
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
        """When assertions are enabled, record the failure count and turn a non-FAIL result into FAIL if any fired."""
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
    """Grades a test from the UVM report summary counts."""

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
        """Return TestResults graded against max_warns, max_errors and zero fatals."""

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
