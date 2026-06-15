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

    Backends differ in what they can measure (openXC7 has no power /
    DRC / methodology reports, Vivado has no single-number Fmax) — every
    metric is optional and a ``None`` simply omits the key, so machine
    consumers must treat all metric keys as optional.

    ``failing_endpoints`` / ``failing_paths`` are the timing-closure
    loop fields: the count of endpoints with negative slack and the
    worst failing paths (``{"slack_ns", "source", "destination", ...}``
    dicts) so an agent can hypothesize a fix without re-parsing reports.
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
        # Vendor methodology findings ({id, severity, description} dicts),
        # surfaced verbatim — informational, never a pass/fail input.
        if methodology_warnings is not None:
            self.results["methodology_warnings"] = methodology_warnings
        # Deliberately set even when None so machine consumers can
        # distinguish "no bitstream requested" from older payloads.
        self.results["bitstream"] = bitstream


class FpgaFailResults(FpgaResults):
    def __init__(self, name, desc, metrics=None):
        results = {"result": "FAIL", "name": name, "desc": desc}
        # A timing-gate failure (require-timing-met) carries the routed
        # metrics forward so a closure loop still sees wns_ns/timing_met/
        # failing_paths on the failing payload.
        if metrics:
            results.update(metrics)
        super().__init__(name=name, results=results)


class FpgaSkipResults(FpgaResults):
    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={"result": "SKIP", "name": name, "desc": desc},
        )
