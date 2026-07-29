# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
import pprint

from .xfail import is_pass_with_xfail


class TestResults:
    """
    Test results
    """

    def __init__(self, name, results={"result": "NA", "desc": "NA"}):
        """
        results from vlog_sim.post()
        """
        self.name = name
        self.results = results

        if "result" not in results:
            results["result"] = "NA"

        if "desc" not in results:
            results["desc"] = "NA"

    def is_pass(self):
        # PASS/SKIP/XFAIL pass; XPASS passes only for a non-strict xfail.
        return is_pass_with_xfail(self.results)

    def to_json_dict(self):
        """JSON-serializable form for per-run result artifacts (#351)."""
        return {
            "kind": type(self).__name__,
            "name": self.name,
            "results": dict(self.results),
        }

    @staticmethod
    def from_json_dict(data):
        """Reconstruct a result from :meth:`to_json_dict` output.

        Always returns a base ``TestResults`` regardless of the original
        subclass: pass/fail semantics (``is_pass``, xfail) live entirely
        in the results dict, and subclasses differ only in how they
        populate it. ``kind`` is carried for reporting, not behavior.
        """
        if not isinstance(data, dict) or not isinstance(data.get("results"), dict):
            raise ValueError("malformed test result record")
        return TestResults(name=data.get("name"), results=dict(data["results"]))

    def __str__(self):
        return "test_results: " + pprint.pformat(self.results)


class TestPassResults(TestResults):
    """
    Generic test pass results
    """

    def __init__(self, name):
        super().__init__(
            name=name,
            results={"result": "PASS", "name": name, "desc": "Generic test pass"},
        )


class CompileFailResults(TestResults):
    """
    Compilation failed
    """

    def __init__(self, name):
        super().__init__(
            name=name,
            results={"result": "FAIL", "name": name, "desc": "Compile failed"},
        )


class EarlyStopResults(TestResults):
    """
    Early Stopping
    """

    def __init__(self, name, desc):
        super().__init__(
            name=name, results={"result": "NA", "name": name, "desc": desc}
        )


class SimTimeoutResults(TestResults):
    """
    Simulation timeout
    """

    def __init__(self, name):
        super().__init__(
            name=name,
            results={"result": "FAIL", "name": name, "desc": "Sim hit timeout"},
        )


class SkipResults(TestResults):
    """
    Test skipped due to regression level
    """

    def __init__(self, name, desc):
        super().__init__(
            name=name, results={"result": "SKIP", "name": name, "desc": desc}
        )


class FilelistFailResults(TestResults):
    """
    Filelist validation failed before compile (bad path, malformed line, missing file, etc.).
    """

    def __init__(self, name, desc):
        super().__init__(
            name=name, results={"result": "FAIL", "name": name, "desc": desc}
        )


class SetupFailResults(TestResults):
    """
    Test setup failed before compile/sim.
    """

    def __init__(self, name, desc):
        super().__init__(
            name=name, results={"result": "FAIL", "name": name, "desc": desc}
        )


class DispatchFailResults(TestResults):
    """
    A dispatched job failed as infrastructure: it was submitted but
    produced no loadable result envelope (killed by the scheduler,
    crashed before writing, or wrote garbage). Never silently dropped —
    the run counts as a FAIL with the collection error in the desc.
    """

    def __init__(self, name, desc):
        super().__init__(
            name=name, results={"result": "FAIL", "name": name, "desc": desc}
        )
