"""Result records for a single style-lint (verible) check run."""

import pprint

from .xfail import FAIL_STAGE_KEY, is_pass_with_xfail


class LintResults:
    def __init__(self, name, results=None):
        if results is None:
            results = {"result": "NA", "desc": "NA"}
        self.name = name
        self.results = results
        if "result" not in results:
            results["result"] = "NA"
        if "desc" not in results:
            results["desc"] = "NA"

    def is_pass(self) -> bool:
        # PASS/SKIP/XFAIL pass; XPASS passes only for a non-strict xfail.
        return is_pass_with_xfail(self.results)

    def __str__(self):
        return "lint_results: " + pprint.pformat(self.results)


class LintPassResults(LintResults):
    def __init__(self, name, *, files: int, excluded: int = 0):
        # A "pass" in style lint means: zero violations over the checked
        # file set. Surface the file and excluded counts so the summary
        # table can show what a clean run actually covered — a check
        # whose excludes ate the interesting files reads as suspiciously
        # cheap right in the table.
        desc = f"clean over {files} file(s)"
        if excluded:
            desc += f" ({excluded} excluded)"
        super().__init__(
            name=name,
            results={"result": "PASS", "name": name, "desc": desc},
        )
        self.results["violations"] = 0
        self.results["files"] = files
        self.results["excluded"] = excluded


class LintFailResults(LintResults):
    """A failed check.

    ``fail_stage`` names a stage that failed *instead of* producing a
    verdict on the design; such a failure is never excused by an xfail
    marker (#553, #594). Leave it unset for the flow's own verdict.
    """

    def __init__(
        self,
        name,
        *,
        violations: int,
        files: int,
        excluded: int = 0,
        desc: str | None = None,
        fail_stage: str | None = None,
    ):
        msg = desc or f"{violations} lint violation(s) over {files} file(s)"
        super().__init__(
            name=name,
            results={"result": "FAIL", "name": name, "desc": msg},
        )
        self.results["violations"] = violations
        self.results["files"] = files
        self.results["excluded"] = excluded
        if fail_stage is not None:
            self.results[FAIL_STAGE_KEY] = fail_stage


class LintSkipResults(LintResults):
    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={"result": "SKIP", "name": name, "desc": desc},
        )
