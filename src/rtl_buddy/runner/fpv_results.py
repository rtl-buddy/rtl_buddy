"""Result records for a single FPV verification run."""

import pprint


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
        return self.results["result"] in ("PASS", "SKIP")

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


class FpvSkipResults(FpvResults):
    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={"result": "SKIP", "name": name, "desc": desc},
        )
