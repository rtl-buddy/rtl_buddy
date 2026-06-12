import pprint

from .xfail import is_pass_with_xfail


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

    ``lut`` / ``ff`` / ``bram`` / ``dsp`` are each a
    ``{"used", "available", "util_pct"}`` dict (the canonical aliases
    from ``fpga_vivado_reports.parse_utilization``). ``bitstream`` is
    always present on a pass — ``None`` when bitstream generation was
    not requested (`rb fpga` without ``--bitstream``).
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
        total_power_w: float | None = None,
        drc_violations: int | None = None,
        drc_by_severity: dict | None = None,
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
        if total_power_w is not None:
            self.results["total_power_w"] = total_power_w
        if drc_violations is not None:
            self.results["drc_violations"] = drc_violations
        if drc_by_severity is not None:
            self.results["drc_by_severity"] = drc_by_severity
        # Deliberately set even when None so machine consumers can
        # distinguish "no bitstream requested" from older payloads.
        self.results["bitstream"] = bitstream


class FpgaFailResults(FpgaResults):
    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={"result": "FAIL", "name": name, "desc": desc},
        )


class FpgaSkipResults(FpgaResults):
    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={"result": "SKIP", "name": name, "desc": desc},
        )
