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
        # XFAIL (an expected failure that did fail) always counts as a pass.
        # XPASS (an expected failure that unexpectedly passed) counts as a
        # pass only for a non-strict xfail; a strict xfail makes XPASS a
        # failure so a stale marker is loud. See apply_xfail().
        result = self.results["result"]
        if result in ("PASS", "SKIP", "XFAIL"):
            return True
        if result == "XPASS":
            return not self.results.get("xfail_strict", False)
        return False

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


def apply_xfail(result: FpvResults, *, strict: bool = False) -> FpvResults:
    """Re-interpret a result under an expected-fail (xfail) verification.

    Mutates and returns ``result`` in place (results are freshly built per
    run, so this is safe):

    - ``FAIL`` -> ``XFAIL`` — the expected failure happened; counts as a
      pass via :meth:`FpvResults.is_pass`, so it does not fail the run.
    - ``PASS`` -> ``XPASS`` — the verification was expected to fail but
      passed. For a non-strict xfail this still counts as a pass; for a
      ``strict`` xfail it counts as a failure, so a stale marker (the
      property started holding) surfaces loudly. ``strict`` is recorded on
      the result so :meth:`FpvResults.is_pass` can honour it.
    - ``SKIP`` / ``NA`` -> unchanged (no verdict to re-interpret).

    Note: like pytest xfail without ``raises=``, this does not distinguish
    a genuine property disproof from an infrastructure error that also
    surfaces as ``FAIL`` — both become ``XFAIL``. Reserve xfail for
    properties whose failure is understood.
    """
    status = result.results.get("result")
    if status == "FAIL":
        result.results["result"] = "XFAIL"
        result.results["desc"] = "xfail (expected fail): " + result.results.get(
            "desc", ""
        )
    elif status == "PASS":
        result.results["result"] = "XPASS"
        result.results["xfail_strict"] = strict
        note = (
            "XPASS (expected fail but passed — strict, failing): "
            if strict
            else "XPASS (expected fail but passed): "
        )
        result.results["desc"] = note + result.results.get("desc", "")
    return result
