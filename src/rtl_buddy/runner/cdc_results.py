"""Result records for a single CDC analysis run."""

import pprint


class CdcResults:
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
        return self.results["result"] in ("PASS", "SKIP")

    def __str__(self):
        return "cdc_results: " + pprint.pformat(self.results)


class CdcPassResults(CdcResults):
    def __init__(
        self,
        name,
        *,
        violations: int = 0,
        suppressed: int = 0,
        crossings: int | None = None,
    ):
        # A "pass" in CDC means: zero unsuppressed violations. We still
        # surface the suppressed count and the crossing total so the
        # summary table can show why a clean run is clean.
        desc = "no rule violations"
        if suppressed:
            desc = f"no rule violations ({suppressed} suppressed)"
        super().__init__(
            name=name,
            results={"result": "PASS", "name": name, "desc": desc},
        )
        self.results["violations"] = violations
        self.results["suppressed"] = suppressed
        if crossings is not None:
            self.results["crossings"] = crossings


class CdcFailResults(CdcResults):
    def __init__(
        self,
        name,
        *,
        violations: int,
        suppressed: int = 0,
        crossings: int | None = None,
        desc: str | None = None,
    ):
        msg = desc or f"{violations} CDC violation(s)"
        super().__init__(
            name=name,
            results={"result": "FAIL", "name": name, "desc": msg},
        )
        self.results["violations"] = violations
        self.results["suppressed"] = suppressed
        if crossings is not None:
            self.results["crossings"] = crossings


class CdcSkipResults(CdcResults):
    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={"result": "SKIP", "name": name, "desc": desc},
        )
