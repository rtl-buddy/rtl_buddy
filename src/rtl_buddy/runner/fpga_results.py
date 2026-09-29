import pprint

from .xfail import FAIL_STAGE_KEY, is_pass_with_xfail


class FpgaResults:
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
        return "fpga_results: " + pprint.pformat(self.results)


class FpgaPassResults(FpgaResults):
    """A passed implementation run with its post-route metrics.

    ``lut``, ``ff``, ``bram`` and ``dsp`` are ``{"used", "available", "util_pct"}`` dicts.
    ``bitstream`` is always present; it is ``None`` when no bitstream was requested.
    Every other metric is optional, and a ``None`` omits the key because backends differ in what they measure.

    ``failing_endpoints`` counts endpoints with negative slack; ``failing_paths`` lists the worst ones as
    ``{"slack_ns", "source", "destination", ...}`` dicts.
    """

    def __init__(
        self,
        name,
        *,
        lut: dict | None = None,
        ff: dict | None = None,
        bram: dict | None = None,
        dsp: dict | None = None,
        wns_ns: float | None = None,
        tns_ns: float | None = None,
        whs_ns: float | None = None,
        timing_met: bool | None = None,
        fmax_mhz: float | None = None,
        failing_endpoints: int | None = None,
        failing_paths: list | None = None,
        total_power_w: float | None = None,
        dynamic_power_w: float | None = None,
        static_power_w: float | None = None,
        drc_violations: int | None = None,
        drc_by_severity: dict | None = None,
        methodology_warnings: list | None = None,
        bitstream: str | None = None,
    ):
        super().__init__(
            name=name,
            results={"result": "PASS", "name": name, "desc": "FPGA flow passed"},
        )
        if lut is not None:
            self.results["lut"] = lut
        if ff is not None:
            self.results["ff"] = ff
        if bram is not None:
            self.results["bram"] = bram
        if dsp is not None:
            self.results["dsp"] = dsp
        if wns_ns is not None:
            self.results["wns_ns"] = wns_ns
        if tns_ns is not None:
            self.results["tns_ns"] = tns_ns
        if whs_ns is not None:
            self.results["whs_ns"] = whs_ns
        if timing_met is not None:
            self.results["timing_met"] = timing_met
        if fmax_mhz is not None:
            self.results["fmax_mhz"] = fmax_mhz
        if failing_endpoints is not None:
            self.results["failing_endpoints"] = failing_endpoints
        if failing_paths is not None:
            self.results["failing_paths"] = failing_paths
        if total_power_w is not None:
            self.results["total_power_w"] = total_power_w
        if dynamic_power_w is not None:
            self.results["dynamic_power_w"] = dynamic_power_w
        if static_power_w is not None:
            self.results["static_power_w"] = static_power_w
        if drc_violations is not None:
            self.results["drc_violations"] = drc_violations
        if drc_by_severity is not None:
            self.results["drc_by_severity"] = drc_by_severity
        if methodology_warnings is not None:
            self.results["methodology_warnings"] = methodology_warnings
        # Set even when None: the key's presence marks the payload as bitstream-aware.
        self.results["bitstream"] = bitstream


class FpgaFailResults(FpgaResults):
    """A failed implementation run.

    Set ``fail_stage`` when a stage failed instead of producing a verdict on the design; an xfail marker never excuses such a failure.
    """

    def __init__(self, name, desc, metrics=None, *, fail_stage: str | None = None):
        results = {"result": "FAIL", "name": name, "desc": desc}
        # A require-timing-met failure keeps the routed metrics.
        if metrics:
            results.update(metrics)
        if fail_stage is not None:
            results[FAIL_STAGE_KEY] = fail_stage
        super().__init__(name=name, results=results)


class FpgaSkipResults(FpgaResults):
    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={"result": "SKIP", "name": name, "desc": desc},
        )
