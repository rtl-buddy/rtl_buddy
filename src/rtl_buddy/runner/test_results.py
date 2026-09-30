# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
import pprint

from .xfail import FAIL_STAGE_KEY, is_pass_with_xfail

# Marks an ``NA`` result as an intentional stop before a verdict (``-E pre|comp|sim``), as opposed to an unknown outcome.
# Only the intentional one may leave the CLI exit code at 0. An envelope without the flag reads as unknown.
EARLY_STOP_KEY = "early_stop"


def is_early_stop(results: dict) -> bool:
    """Return whether a results dict records an intentional early stop."""
    return bool(results.get(EARLY_STOP_KEY))


def is_run_failure(result) -> bool:
    """Return whether one result makes the run fail; this is the exit-code rule.

    A pass (``PASS``, ``SKIP``, ``XFAIL``, non-strict ``XPASS``) never fails the run. An ``NA`` fails unless it is an intentional early stop.
    The in-process head and a dispatched ``rb _test-job`` both use it.
    """
    if result.is_pass():
        return False
    return not (result.results.get("result") == "NA" and is_early_stop(result.results))


class TestResults:
    """Base record of one test's outcome."""

    def __init__(self, name, results={"result": "NA", "desc": "NA"}):
        """``results`` is the dict from vlog_sim.post(); missing ``result`` and ``desc`` default to ``NA``."""
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
        """Return the JSON-serializable form used in result envelopes."""
        return {
            "kind": type(self).__name__,
            "name": self.name,
            "results": dict(self.results),
        }

    @staticmethod
    def from_json_dict(data):
        """Reconstruct a result from :meth:`to_json_dict` output.

        Always returns a base ``TestResults``; the subclass in ``kind`` does not affect pass/fail semantics.
        """
        if not isinstance(data, dict) or not isinstance(data.get("results"), dict):
            raise ValueError("malformed test result record")
        return TestResults(name=data.get("name"), results=dict(data["results"]))

    def __str__(self):
        return "test_results: " + pprint.pformat(self.results)


class TestPassResults(TestResults):
    """A generic test pass."""

    def __init__(self, name):
        super().__init__(
            name=name,
            results={"result": "PASS", "name": name, "desc": "Generic test pass"},
        )


# The head compares against this desc to find rows that still need the build job's real error.
COMPILE_FAIL_DESC = "Compile failed"


class CompileFailResults(TestResults):
    """Compilation failed. An xfail marker never excuses it.

    ``desc`` replaces the generic wording when the real error is known; keep it to one line.
    """

    def __init__(self, name, desc=None):
        super().__init__(
            name=name,
            results={
                "result": "FAIL",
                "name": name,
                "desc": desc or COMPILE_FAIL_DESC,
                FAIL_STAGE_KEY: "compile",
            },
        )


class EarlyStopResults(TestResults):
    """The run stopped before a verdict because ``-E pre|comp|sim`` asked it to.

    Its ``NA`` carries :data:`EARLY_STOP_KEY`, which exempts the row from a failing exit code.
    """

    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={
                "result": "NA",
                "name": name,
                "desc": desc,
                EARLY_STOP_KEY: True,
            },
        )


class SimTimeoutResults(TestResults):
    """Simulation timeout. An xfail marker never excuses it."""

    def __init__(self, name):
        super().__init__(
            name=name,
            results={
                "result": "FAIL",
                "name": name,
                "desc": "Sim hit timeout",
                FAIL_STAGE_KEY: "sim_timeout",
            },
        )


class SimStageFailResults(TestResults):
    """The simulation stage failed under ``-E sim``. An xfail marker never excuses it.

    ``-E sim`` skips post-processing, so nothing parsed the transcript; the desc names the simulator exit status rather than a missing verdict.
    """

    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={
                "result": "FAIL",
                "name": name,
                "desc": desc,
                FAIL_STAGE_KEY: "sim",
            },
        )


class SkipResults(TestResults):
    """Test skipped because of its regression level."""

    def __init__(self, name, desc):
        super().__init__(
            name=name, results={"result": "SKIP", "name": name, "desc": desc}
        )


class FilelistFailResults(TestResults):
    """Filelist validation failed before compile (bad path, malformed line, missing file). An xfail marker never excuses it."""

    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={
                "result": "FAIL",
                "name": name,
                "desc": desc,
                FAIL_STAGE_KEY: "setup",
            },
        )


class SetupFailResults(TestResults):
    """Test setup failed before compile or sim. An xfail marker never excuses it."""

    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={
                "result": "FAIL",
                "name": name,
                "desc": desc,
                FAIL_STAGE_KEY: "setup",
            },
        )


class DispatchFailResults(TestResults):
    """A dispatched job produced no loadable result envelope (killed, crashed or wrote garbage).

    The run counts as a FAIL with the collection error in the desc. An xfail marker never excuses it.
    """

    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={
                "result": "FAIL",
                "name": name,
                "desc": desc,
                FAIL_STAGE_KEY: "dispatch",
            },
        )
