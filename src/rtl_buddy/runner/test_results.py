# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
import pprint

from .xfail import FAIL_STAGE_KEY, is_pass_with_xfail

# Marks an ``NA`` result as an *intentional* stop before a verdict — a
# run_depth early stop (``-E pre|comp|sim``, #336) — as opposed to a run
# whose outcome is simply unknown (no PASS/FAIL banner in the transcript,
# an aborted simulator). Both spell themselves ``result: "NA"``, and only
# the intentional one may leave the CLI exit code at 0 (#546).
#
# Additive: the envelope's ``schema_version`` is unchanged, ``to_json_dict``
# carries it through the results dict for free, and an envelope written by
# an older rtl_buddy — which has no flag — reads as "unknown", the safe
# side of the distinction.
EARLY_STOP_KEY = "early_stop"


def is_early_stop(results: dict) -> bool:
    """Whether a results dict records an intentional early stop (#546)."""
    return bool(results.get(EARLY_STOP_KEY))


def is_run_failure(result) -> bool:
    """Whether one result makes the run fail — the exit-code rule (#546).

    A pass (``PASS`` / ``SKIP`` / ``XFAIL`` / non-strict ``XPASS``) never
    fails the run. Of the rest, only an ``NA`` that says it stopped early
    on purpose is exempt: an ``NA`` meaning "no verdict was produced" is
    an unknown outcome and must fail, exactly as ``is_pass()`` already
    reports it.

    The single grading rule for both the in-process head
    (``_exit_code_from_results``) and a dispatched ``rb _test-job``, so a
    run cannot be graded one way locally and another over a scheduler.
    """
    if result.is_pass():
        return False
    return not (result.results.get("result") == "NA" and is_early_stop(result.results))


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


# The desc a compile failure carries when nothing more specific is known.
# A module constant because it is also a *predicate* on the collecting head
# (#498): a dispatched sim job's envelope saying exactly this is what marks
# the row as "the compile failed, and nobody said why yet", which is the row
# the build job's real error gets folded into.
COMPILE_FAIL_DESC = "Compile failed"


class CompileFailResults(TestResults):
    """
    Compilation failed

    ``desc`` overrides the generic wording when the caller knows the real
    error — a dispatched sim job gated on a build job whose compile already
    failed carries that build's exit status and error lines here, so the run
    summary shows the design error instead of a bare ``Compile failed``
    (#498). Keep it to ONE line: the summary tables render it in a cell.

    Never an expected failure: no simulation ran, so an xfail marker on
    this test has nothing to be about (#553).
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
    """
    Early Stopping

    The run stopped before a verdict because it was asked to
    (``-E pre|comp|sim``), so its ``NA`` is intentional and carries
    :data:`EARLY_STOP_KEY` to say so — that flag, not the bare ``NA``, is
    what exempts the row from the exit code (#546).
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
    """
    Simulation timeout

    Never an expected failure: the simulation was cut off before it could
    report a verdict, so a marked test that times out has not shown the
    failure it is marked for (#594).
    """

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
    """The simulation stage itself failed under ``-E sim``.

    ``-E sim`` runs the simulation and stops before post-processing, so a
    nonzero simulator exit there is a failed stage rather than the
    successful early stop it used to report (#546). Nothing parsed the
    transcript — the user asked to skip that — so the desc names the exit
    status and says so, instead of claiming a verdict is missing
    (#574 review): a transcript can very well carry a FAIL banner here.

    Never an expected failure: nothing read a verdict out of this run, so
    an xfail marker has nothing to excuse (#594).
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

    Never an expected failure: the run stopped before the design was even
    compiled (#553).
    """

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
    """
    Test setup failed before compile/sim.

    Never an expected failure: the run stopped before the behaviour an
    xfail marker is about could be exercised (#553).
    """

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
    """
    A dispatched job failed as infrastructure: it was submitted but
    produced no loadable result envelope (killed by the scheduler,
    crashed before writing, or wrote garbage). Never silently dropped —
    the run counts as a FAIL with the collection error in the desc, and
    never an expected one: infrastructure is not what a marker is about
    (#594).
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
