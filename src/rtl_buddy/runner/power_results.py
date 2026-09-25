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


#: How many cell names the pass description spells out before it stops
#: counting. A design that lost a whole library would otherwise put
#: hundreds of names in a results table cell; the machine fields carry
#: the complete list either way.
_DESC_CELLS = 3


def _unpowered_qualifier(cells, instance_count: int) -> str:
    """What to add to a pass description when cells had no library (#627).

    The verdict is unchanged — the watts reported are a real measurement
    of everything that had a library — so the qualifier is what stops
    them from being read as a measurement of the whole design. It names
    the cells because the cell is what the reader has to go and supply a
    Liberty for; which instances they are is in the per-instance report
    and in the model beside it.
    """
    if not cells:
        return ""
    listed = ", ".join(str(c) for c in cells[:_DESC_CELLS])
    if len(cells) > _DESC_CELLS:
        listed += f", +{len(cells) - _DESC_CELLS} more"
    one = instance_count == 1
    instance_word = "instance" if one else "instances"
    cell_word = "cell" if len(cells) == 1 else "cells"
    return (
        f"; {instance_count} {instance_word} of {len(cells)} {cell_word} "
        f"with no Liberty power data {'reports' if one else 'report'} 0 W "
        f"({listed})"
    )


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
        parasitics: str | None = None,
        phys_model: str | None = None,
        unpowered_cells: list | None = None,
        unpowered_instance_count: int = 0,
        worst_corner: str | None = None,
        corners: dict | None = None,
    ):
        unpowered_cells = list(unpowered_cells or [])
        super().__init__(
            name=name,
            results={
                "result": "PASS",
                "name": name,
                "desc": "Power analysis passed"
                + _unpowered_qualifier(unpowered_cells, unpowered_instance_count),
            },
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
        # What a post-P&R analysis timed the routing on: "spef" (the P&R
        # run's OpenRCX extraction) or "estimated" (global-route estimate).
        # Absent on a synth-source run, which has no routing (#101).
        if parasitics is not None:
            self.results["parasitics"] = parasitics
        # Multi-corner signoff (#104, #105): the watts above are the worst
        # (highest-total) corner's, named here, and `corners` carries each
        # corner's own four. Both absent on a single-corner run.
        if worst_corner is not None:
            self.results["worst_corner"] = worst_corner
        if corners:
            self.results["corners"] = corners
        # Where the per-instance breakdown behind these scalars was written
        # (#558). Absent when the run could not publish one.
        if phys_model is not None:
            self.results["phys_model"] = phys_model
        # The cells the analysis had no library for, and how many
        # instances of them there are (#627). Absent — rather than empty —
        # on the ordinary run, so a consumer reading the key reads a run
        # that found something.
        if unpowered_cells:
            self.results["unpowered_cells"] = unpowered_cells
            self.results["unpowered_cell_count"] = len(unpowered_cells)
            self.results["unpowered_instance_count"] = unpowered_instance_count


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
