import pprint

from .xfail import FAIL_STAGE_KEY, is_pass_with_xfail


class PowerResults:
    def __init__(self, name, results=None):
        if results is None:
            results = {"result": "NA", "desc": "NA"}
        self.name = name
        self.results = results
        if "result" not in results:
            results["result"] = "NA"
        if "desc" not in results:
            results["desc"] = "NA"

    def is_pass(self):
        # PASS/SKIP/XFAIL pass; XPASS passes only for a non-strict xfail.
        return is_pass_with_xfail(self.results)

    def __str__(self):
        return "power_results: " + pprint.pformat(self.results)


class PowerPassResults(PowerResults):
    def __init__(
        self,
        name,
        *,
        mode: str | None = None,
        netlist_source: str | None = None,
        total_w: float | None = None,
        internal_w: float | None = None,
        switching_w: float | None = None,
        leakage_w: float | None = None,
        activity_source: str | None = None,
        phys_model: str | None = None,
    ):
        super().__init__(
            name=name,
            results={"result": "PASS", "name": name, "desc": "Power analysis passed"},
        )
        if mode is not None:
            self.results["mode"] = mode
        if netlist_source is not None:
            self.results["netlist_source"] = netlist_source
        if total_w is not None:
            self.results["total_w"] = total_w
        if internal_w is not None:
            self.results["internal_w"] = internal_w
        if switching_w is not None:
            self.results["switching_w"] = switching_w
        if leakage_w is not None:
            self.results["leakage_w"] = leakage_w
        if activity_source is not None:
            self.results["activity_source"] = activity_source
        # Where the per-instance breakdown behind these scalars was written
        # (#558). Absent when the run could not publish one.
        if phys_model is not None:
            self.results["phys_model"] = phys_model


class PowerFailResults(PowerResults):
    """A failed run.

    ``fail_stage`` names a stage that failed *instead of* producing a
    verdict on the design (a missing tool, an unresolvable platform, a
    filelist error). Such a failure is never excused by an xfail marker
    (#553, #594); leave it unset for the flow's own verdict.
    """

    def __init__(self, name, desc, *, fail_stage: str | None = None):
        results = {"result": "FAIL", "name": name, "desc": desc}
        if fail_stage is not None:
            results[FAIL_STAGE_KEY] = fail_stage
        super().__init__(name=name, results=results)


class PowerSkipResults(PowerResults):
    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={"result": "SKIP", "name": name, "desc": desc},
        )
