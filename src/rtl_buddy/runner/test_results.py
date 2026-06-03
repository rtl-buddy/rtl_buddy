# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
import pprint


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


def apply_xfail(result: TestResults, *, strict: bool = False) -> TestResults:
    """Re-interpret a test result under an expected-fail (xfail) test.

    Mutates and returns ``result`` in place:

    - ``FAIL`` -> ``XFAIL`` — the expected failure happened; counts as a
      pass via :meth:`TestResults.is_pass`, so it does not fail the run.
    - ``PASS`` -> ``XPASS`` — the test was expected to fail but passed. For
      a non-strict xfail this still counts as a pass; for a ``strict`` xfail
      it counts as a failure so a stale marker is loud. ``strict`` is
      recorded on the result so :meth:`TestResults.is_pass` can honour it.
    - ``SKIP`` / ``NA`` -> unchanged.

    Like pytest xfail without ``raises=``, this does not distinguish a
    genuine test failure from a setup/compile error that also surfaces as
    ``FAIL``. Reserve xfail for tests whose failure is understood.
    """
    status = result.results.get("result")
    if status == "FAIL":
        result.results["result"] = "XFAIL"
        result.results["desc"] = "xfail (expected fail): " + str(
            result.results.get("desc", "")
        )
    elif status == "PASS":
        result.results["result"] = "XPASS"
        result.results["xfail_strict"] = strict
        note = (
            "XPASS (expected fail but passed — strict, failing): "
            if strict
            else "XPASS (expected fail but passed): "
        )
        result.results["desc"] = note + str(result.results.get("desc", ""))
    return result
