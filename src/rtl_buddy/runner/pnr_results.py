import pprint

from .xfail import FAIL_STAGE_KEY, is_pass_with_xfail


class PnrResults:
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
        return "pnr_results: " + pprint.pformat(self.results)


class PnrPassResults(PnrResults):
    def __init__(
        self,
        name,
        *,
        area_um2: float | None = None,
        cell_count: int | None = None,
        wns_setup_ps: float | None = None,
        wns_hold_ps: float | None = None,
        tns_ps: float | None = None,
        drc_count: int | None = None,
        gds_path: str | None = None,
        png_path: str | None = None,
    ):
        super().__init__(
            name=name,
            results={"result": "PASS", "name": name, "desc": "P&R passed"},
        )
        if area_um2 is not None:
            self.results["area_um2"] = area_um2
        if cell_count is not None:
            self.results["cell_count"] = cell_count
        if wns_setup_ps is not None:
            self.results["wns_setup_ps"] = wns_setup_ps
        if wns_hold_ps is not None:
            self.results["wns_hold_ps"] = wns_hold_ps
        if tns_ps is not None:
            self.results["tns_ps"] = tns_ps
        if drc_count is not None:
            self.results["drc_count"] = drc_count
        if gds_path is not None:
            self.results["gds_path"] = gds_path
        if png_path is not None:
            self.results["png_path"] = png_path


class PnrFailResults(PnrResults):
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


class PnrSkipResults(PnrResults):
    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={"result": "SKIP", "name": name, "desc": desc},
        )
