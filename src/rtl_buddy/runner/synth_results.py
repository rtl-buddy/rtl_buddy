import pprint

from .xfail import FAIL_STAGE_KEY, is_pass_with_xfail


class SynthResults:
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
        return "synth_results: " + pprint.pformat(self.results)


class SynthPassResults(SynthResults):
    def __init__(
        self,
        name,
        *,
        area_um2: float | None = None,
        gate_count: int | None = None,
        wns_ps: float | None = None,
        tns_ps: float | None = None,
        static_function_findings: int | None = None,
        unresolved_interfaces: int | None = None,
        phys_model: str | None = None,
    ):
        super().__init__(
            name=name,
            results={"result": "PASS", "name": name, "desc": "Synthesis passed"},
        )
        if area_um2 is not None:
            self.results["area_um2"] = area_um2
        if gate_count is not None:
            self.results["gate_count"] = gate_count
        if wns_ps is not None:
            self.results["wns_ps"] = wns_ps
        if tns_ps is not None:
            self.results["tns_ps"] = tns_ps
        # Present only when the static-lifetime gate ran in `warn` mode and
        # found something: a passing run whose netlist may still be wrong.
        if static_function_findings:
            self.results["static_function_findings"] = static_function_findings
        # Present only when the interface gate ran in `warn` mode and found
        # something: a passing run whose netlist is missing the port
        # connections of that many interface instances.
        if unresolved_interfaces:
            self.results["unresolved_interfaces"] = unresolved_interfaces
        # Where the per-module breakdown behind these scalars was written
        # (#558). Absent when the run could not publish one.
        if phys_model is not None:
            self.results["phys_model"] = phys_model


class SynthFailResults(SynthResults):
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


class SynthSkipResults(SynthResults):
    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={"result": "SKIP", "name": name, "desc": desc},
        )
