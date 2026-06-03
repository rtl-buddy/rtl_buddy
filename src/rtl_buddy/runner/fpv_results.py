"""Result records for a single FPV verification run."""

import pprint

from .xfail import is_pass_with_xfail


class FpvResults:
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
        return "fpv_results: " + pprint.pformat(self.results)


class FpvPassResults(FpvResults):
    def __init__(
        self,
        name,
        *,
        mode: str,
        depth: int,
        engines: list[str] | None = None,
        runtime_s: float | None = None,
        per_engine: list[dict] | None = None,
    ):
        desc = f"property proved ({mode}, depth {depth})"
        super().__init__(
            name=name,
            results={"result": "PASS", "name": name, "desc": desc},
        )
        self.results["mode"] = mode
        self.results["depth"] = depth
        self.results["engines"] = list(engines) if engines is not None else []
        if runtime_s is not None:
            self.results["runtime_s"] = runtime_s
        # per_engine carries the parsed `summary: engine_<N> ...`
        # lines from sby's logfile.txt: list of dicts with idx, spec,
        # verdict, trace_count. Empty when no logfile was produced.
        self.results["per_engine"] = list(per_engine) if per_engine is not None else []


class FpvFailResults(FpvResults):
    def __init__(
        self,
        name,
        *,
        mode: str,
        depth: int,
        engines: list[str] | None = None,
        runtime_s: float | None = None,
        desc: str | None = None,
        per_engine: list[dict] | None = None,
    ):
        msg = desc or f"property disproved ({mode}, depth {depth})"
        super().__init__(
            name=name,
            results={"result": "FAIL", "name": name, "desc": msg},
        )
        self.results["mode"] = mode
        self.results["depth"] = depth
        self.results["engines"] = list(engines) if engines is not None else []
        if runtime_s is not None:
            self.results["runtime_s"] = runtime_s
        self.results["per_engine"] = list(per_engine) if per_engine is not None else []


class FpvSkipResults(FpvResults):
    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={"result": "SKIP", "name": name, "desc": desc},
        )
