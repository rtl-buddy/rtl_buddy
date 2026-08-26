"""CLI tests for the hidden ``rb _test-job`` re-entry command (#351 P0).

The command is the unit a remote dispatch backend submits: run one
(test, run_id) and write a ``result.json`` envelope for the collecting
head process. ``TestRunner`` is stubbed out so no real simulator is
needed; the ``minimal_project`` fixture provides the config surface.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import rtl_buddy.rtl_buddy as rtl_buddy_module
from rtl_buddy.dispatch.argv import job_log_path
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.config import SuiteConfig
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.result_io import load_result_json
from rtl_buddy.runner.test_results import (
    CompileFailResults,
    TestPassResults,
)
from rtl_buddy.seed_mode import SeedMode


class _StubTestRunner:
    """Stands in for TestRunner: records ctor args, returns a canned result.

    Implements the split contract the build job drives since #495
    (``prepare`` → ``compile_group_dir`` → ``compile_prepared``) as well as
    ``run``/``run_multiple``, so the same stub serves both the ``_test-job``
    tests and the ``_build-job`` ones. ``inits`` (and the thread each
    construction happened on) is append-only under a lock because the
    compile phase is threaded; ``last_init`` is kept for the assertions that
    only care that *a* runner was built the right way.
    """

    canned = None
    last_init = None
    inits: list = []
    init_threads: list = []
    lock = threading.Lock()
    # test name -> group dir. Default: every test is its own group, which is
    # what distinct compile keys look like to the build job.
    group_of = None
    # test name -> Results (or raise). Default: the canned result.
    compile_hook = None
    # test name -> SetupFailResults / None (or raise). Default: PRE passes.
    prepare_hook = None
    # test name -> Results / None: a probe failure, before any group dir.
    group_fail = None
    # test name -> compile record dict / None, the shape VlogSim stamps on
    # itself (#495). Default: a plausible record so the envelope's `builds`
    # list is exercised without every test having to opt in.
    compile_record_of = None

    def __init__(self, **kwargs):
        type(self).last_init = kwargs
        self.test_name = kwargs["test_cfg"].get_name()
        with type(self).lock:
            type(self).inits.append(kwargs)
            type(self).init_threads.append(threading.current_thread().name)

    def run(self):
        return type(self).canned

    def run_multiple(self, run_ids):
        return [type(self).canned for _ in run_ids]

    # ---- the build job's phases

    def prepare(self, **_kwargs):
        hook = type(self).prepare_hook
        return None if hook is None else hook(self.test_name)

    def compile_group_dir(self):
        group_fail = type(self).group_fail
        if group_fail is not None:
            results = group_fail(self.test_name)
            if results is not None:
                return None, results
        group_of = type(self).group_of
        return (self.test_name if group_of is None else group_of(self.test_name)), None

    def compile_prepared(self, run_ids=None):
        hook = type(self).compile_hook
        if hook is None:
            return type(self).canned
        return hook(self.test_name)

    @property
    def last_compile(self):
        record_of = type(self).compile_record_of
        if record_of is None:
            return {
                "duration_sec": 1.5,
                "builder": "stub-builder",
                "reused": False,
            }
        return record_of(self.test_name)

    @property
    def builder_name(self):
        # Known from the moment the sim exists, i.e. before any compile
        # plan — the fallback the build job uses for a config whose PRE
        # failed (#495).
        return "stub-builder"


@pytest.fixture
def stub_runner(monkeypatch: pytest.MonkeyPatch) -> type[_StubTestRunner]:
    _StubTestRunner.canned = None
    _StubTestRunner.last_init = None
    _StubTestRunner.inits = []
    _StubTestRunner.init_threads = []
    _StubTestRunner.group_of = None
    _StubTestRunner.compile_hook = None
    _StubTestRunner.prepare_hook = None
    _StubTestRunner.group_fail = None
    _StubTestRunner.compile_record_of = None
    monkeypatch.setattr(rtl_buddy_module, "TestRunner", _StubTestRunner)
    return _StubTestRunner


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_test_job")


def test_test_job_writes_pass_result_and_exits_0(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    stub_runner.canned = TestPassResults(name="basic/results")
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["_test-job", "basic", "--result-json", "res.json"])
    assert result.exit_code == 0, result.output

    envelope = load_result_json(minimal_project / "res.json")
    assert envelope["test"] == "basic"
    assert envelope["run_id"] is None
    assert envelope["result"].is_pass()


def test_test_job_failing_result_still_writes_json_and_exits_1(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    stub_runner.canned = CompileFailResults(name="basic/results")
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["_test-job", "basic", "--result-json", "res.json"])
    assert result.exit_code == 1, result.output

    envelope = load_result_json(minimal_project / "res.json")
    assert not envelope["result"].is_pass()
    assert envelope["result"].results["result"] == "FAIL"


def test_test_job_token_read_failure_still_writes_result(
    minimal_project: Path,
    stub_runner: type[_StubTestRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """A run_token read that fails must NOT abort after the sim ran — that
    would lose a completed (possibly passing) test's result and report it as
    'produced no result', the exact #362 signature through another door. The
    read is non-fatal: the envelope is still written (with a null token, so
    the head rejects it as stale rather than trusting a mismatched result)."""
    from rtl_buddy.dispatch.plan import write_plan

    suite_cfg = SuiteConfig(path="tests.yaml")
    plan = write_plan(
        minimal_project / "plan.json", "tests.yaml", suite_cfg.get_tests(), "tok"
    )

    # Plan resolves the config fine, but the token read blows up (e.g. the
    # manifest went unreadable on the shared mount between the two reads).
    def boom(_path):
        raise FatalRtlBuddyError("plan vanished mid-run")

    monkeypatch.setattr(rtl_buddy_module, "read_plan_token", boom)

    stub_runner.canned = TestPassResults(name="basic/results")
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["_test-job", "basic", "--result-json", "res.json", "--plan", str(plan)],
    )
    assert result.exit_code == 0, result.output

    envelope = load_result_json(minimal_project / "res.json")
    assert envelope["result"].is_pass()
    assert envelope["run_token"] is None


def test_test_job_unknown_test_exits_nonzero_without_json(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["_test-job", "nope", "--result-json", "res.json"])
    assert result.exit_code != 0
    assert not (minimal_project / "res.json").exists()


def test_test_job_passes_run_id_and_seed_mode_through(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    stub_runner.canned = TestPassResults(name="basic/results")
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "_test-job",
            "basic",
            "--result-json",
            "res.json",
            "--run-id",
            "3",
            "--seed-mode",
            "new",
        ],
    )
    assert result.exit_code == 0, result.output
    assert stub_runner.last_init["run_id"] == 3
    assert stub_runner.last_init["seed_mode"] == SeedMode.NEW
    # Regression parity: job output stays out of the collector's stdout.
    assert stub_runner.last_init["test_runner_mode"] == {"sim_to_stdout": False}

    envelope = load_result_json(minimal_project / "res.json")
    assert envelope["run_id"] == 3


def test_test_job_replay_defaults_replay_run_id_to_run_id(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    stub_runner.canned = TestPassResults(name="basic/results")
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "_test-job",
            "basic",
            "--result-json",
            "res.json",
            "--run-id",
            "2",
            "--seed-mode",
            "replay",
        ],
    )
    assert result.exit_code == 0, result.output
    assert stub_runner.last_init["replay_run_id"] == 2


def test_test_job_machine_envelope(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    stub_runner.canned = TestPassResults(name="basic/results")
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["--machine", "_test-job", "basic", "--result-json", "res.json"]
    )
    assert result.exit_code == 0, result.output

    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    envelope = json.loads(payload_line)
    assert envelope["command"] == "_test-job"
    assert envelope["exit_code"] == 0
    assert envelope["payload"]["result"]["name"] == "basic"
    assert envelope["payload"]["result"]["result"] == "PASS"
    assert envelope["payload"]["result_json"].endswith("res.json")


def test_test_job_hidden_from_help(minimal_project: Path):
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["--help"])
    assert result.exit_code == 0
    assert "_test-job" not in result.output


class _NamedCfg:
    def __init__(self, name):
        self.name = name

    def get_name(self):
        return self.name


def test_resolve_job_test_cfg_expansion_paths(
    minimal_project: Path, monkeypatch: pytest.MonkeyPatch
):
    """Sweep-aware name resolution: base names, expanded names, ambiguity."""
    rb = RtlBuddy(name="resolve_test")
    suite_cfg = SuiteConfig(path="tests.yaml")

    def fake_expand(test_cfg, suite_dir):
        # "basic" sweep-expands into two variants; "extra" is untouched.
        if test_cfg.name == "basic":
            return [_NamedCfg("basic_small"), _NamedCfg("basic_big")], None
        return [test_cfg], None

    monkeypatch.setattr(rb, "_expand_tests_with_sweep", fake_expand)

    cfg, err = rb._resolve_job_test_cfg(suite_cfg, "basic_big", ".")
    assert err is None and cfg.name == "basic_big"

    cfg, err = rb._resolve_job_test_cfg(suite_cfg, "extra", ".")
    assert err is None and cfg.name == "extra"

    with pytest.raises(FatalRtlBuddyError, match="expands to multiple"):
        rb._resolve_job_test_cfg(suite_cfg, "basic", ".")

    with pytest.raises(FatalRtlBuddyError, match="not found"):
        rb._resolve_job_test_cfg(suite_cfg, "nope", ".")


def test_resolve_job_test_cfg_from_plan_skips_hook(
    minimal_project: Path, monkeypatch: pytest.MonkeyPatch
):
    """With --plan, a sim job reads its config from the manifest and never
    runs the suite's sweep hook."""
    from rtl_buddy.dispatch.plan import write_plan

    rb = RtlBuddy(name="resolve_test")
    suite_cfg = SuiteConfig(path="tests.yaml")
    plan = write_plan(
        minimal_project / "plan.json", "tests.yaml", suite_cfg.get_tests(), "tok"
    )

    def boom(test_cfg, suite_dir):  # would run the hook — must not be called
        raise AssertionError("sweep hook must not run when --plan resolves the name")

    monkeypatch.setattr(rb, "_expand_tests_with_sweep", boom)

    cfg, err = rb._resolve_job_test_cfg(suite_cfg, "extra", ".", plan_path=str(plan))
    assert err is None and cfg.get_name() == "extra"


def test_resolve_job_test_cfg_plan_miss_falls_back_to_hook(
    minimal_project: Path, monkeypatch: pytest.MonkeyPatch
):
    """A name absent from the plan still resolves via expansion — the plan
    is an optimization, not a hard dependency."""
    from rtl_buddy.dispatch.plan import write_plan

    rb = RtlBuddy(name="resolve_test")
    suite_cfg = SuiteConfig(path="tests.yaml")
    # Plan holds only "basic"; "extra" must fall through to the hook path.
    plan = write_plan(
        minimal_project / "plan.json",
        "tests.yaml",
        [t for t in suite_cfg.get_tests() if t.get_name() == "basic"],
        "tok",
    )

    cfg, err = rb._resolve_job_test_cfg(suite_cfg, "extra", ".", plan_path=str(plan))
    assert err is None and cfg.get_name() == "extra"


def test_resolve_job_test_cfg_sweep_failure_becomes_setup_error(
    minimal_project: Path, monkeypatch: pytest.MonkeyPatch
):
    rb = RtlBuddy(name="resolve_test")
    suite_cfg = SuiteConfig(path="tests.yaml")

    def broken_expand(test_cfg, suite_dir):
        return [], f"Setup failed in sweep: boom ({test_cfg.name})"

    monkeypatch.setattr(rb, "_expand_tests_with_sweep", broken_expand)

    cfg, err = rb._resolve_job_test_cfg(suite_cfg, "basic", ".")
    assert cfg is None and "Setup failed in sweep" in err

    # An unknown name with broken sweeps reports the sweep failure (the
    # name may have come from the failed expansion) instead of raising.
    cfg, err = rb._resolve_job_test_cfg(suite_cfg, "mystery", ".")
    assert cfg is None and "Setup failed in sweep" in err


# ------------------------------------------------ rb _build-job (#351)


def test_build_job_compiles_runnable_tests(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    from rtl_buddy.runner.test_results import EarlyStopResults

    # A COMP early-stop means "compiled OK" for the build job.
    stub_runner.canned = EarlyStopResults(name="b/results", desc="Stopped at compile")
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["--machine", "_build-job", "-c", "tests.yaml", "-l", "5"]
    )
    assert result.exit_code == 0, result.output
    # run_depth=COMP + share_build on the build TestRunner.
    assert stub_runner.last_init["run_depth"].value == "comp"
    assert stub_runner.last_init["share_build"] is True

    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    envelope = json.loads(payload_line)
    assert envelope["command"] == "_build-job"
    # basic + extra both at/under -l 5.
    assert set(envelope["payload"]["built"]) == {"basic", "extra"}
    assert envelope["payload"]["failed"] == []


def test_build_job_accepts_parallel_and_still_builds_every_config(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """``--parallel N`` is the head's concurrency budget, not a filter (#495).

    Whatever the job does with the budget, the set of configs it compiles
    and the envelope it writes are the ones the serial loop produced.
    """
    from rtl_buddy.runner.test_results import EarlyStopResults

    stub_runner.canned = EarlyStopResults(name="b/results", desc="Stopped at compile")
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["--machine", "_build-job", "-c", "tests.yaml", "-l", "5", "--parallel", "2"],
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    envelope = json.loads(payload_line)
    assert set(envelope["payload"]["built"]) == {"basic", "extra"}
    assert envelope["payload"]["failed"] == []
    # The budget the job was handed is on its own command record: a build
    # job's wall clock is unreadable without the concurrency it ran at.
    build_job = [
        record
        for record in _records(minimal_project / "rtl_buddy.log")
        if record.get("event") == "command.build_job"
    ]
    assert [record.get("parallel") for record in build_job] == [2]


@pytest.mark.parametrize("value", ["0", "-1"])
def test_build_job_rejects_parallel_below_one(
    minimal_project: Path, stub_runner: type[_StubTestRunner], value: str
):
    """A budget of zero builds is a setup error, and it is fatal.

    Fatal is safe *here* only because nothing has compiled yet: the flag is
    checked before the command context is entered, so the exit-0 contract
    that keeps the afterok fan-out alive is never in play.
    """
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["_build-job", "-c", "tests.yaml", "--parallel", value]
    )
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "--parallel must be >= 1" in str(result.exception)


def _build_payload(output: str) -> dict:
    payload_line = [line for line in output.splitlines() if line.startswith("{")][-1]
    return json.loads(payload_line)["payload"]


def _add_third_test(project: Path, name: str = "gamma") -> None:
    """Append a third runnable test to the *tmp copy* of the fixture suite.

    Two configs cannot tell plan order from group order: whatever the
    grouping, the pool yields them in plan order anyway. Three can — one
    group holding the first and last config makes the two orders differ.
    """
    tests_yaml = project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text()
        + f"""  - name: {name}
    desc: third test entry, so plan order and group order can disagree
    model: example
    model_path: models.yaml
    reglvl: 0
    plusargs:
    plusdefines:
    uvm:
    preproc:
    postproc:
    sweep:
    testbench: tb_basic
    sim_timeout:
"""
    )


def test_build_job_compiles_distinct_groups_at_the_same_time(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """Two distinct compile keys really do compile concurrently (#495).

    The proof is a rendezvous, not a stopwatch: each stub compile blocks on
    a 2-party barrier, so a serial build job deadlocks until the timeout and
    both configs come back failed. Both ``built`` is only reachable if the
    two were inside ``compile_prepared`` at the same moment.
    """
    from rtl_buddy.runner.test_results import EarlyStopResults

    barrier = threading.Barrier(2, timeout=15)

    def rendezvous(name):
        barrier.wait()
        return EarlyStopResults(name=f"{name}/results", desc="Stopped at compile")

    stub_runner.compile_hook = rendezvous  # group_of default: one group each
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["--machine", "_build-job", "-c", "tests.yaml", "-l", "5", "--parallel", "2"],
    )
    assert result.exit_code == 0, result.output
    payload = _build_payload(result.output)
    assert payload["built"] == ["basic", "extra"], payload
    assert payload["failed"] == []

    # One TestRunner per plan config, every one of them built on the main
    # thread: construction is immediately followed by prepare(), and PRE is
    # process-global-serial by contract (hooks.py) however wide the pool is.
    assert [init["test_cfg"].get_name() for init in stub_runner.inits] == [
        "basic",
        "extra",
    ]
    assert set(stub_runner.init_threads) == {threading.main_thread().name}

    done = [
        record
        for record in _records(minimal_project / "rtl_buddy.log")
        if record.get("event") == "build_job.done"
    ]
    assert [(r["parallel"], r["groups"]) for r in done] == [(2, 2)]


def test_build_job_never_runs_two_builders_in_one_directory(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """Same compile key ⇒ same group ⇒ strictly serial (#369).

    Two writers in one build directory is corruption, not slowness, so the
    grouping is on the directory the compile *writes*, and a group's members
    are never in flight together however large the budget is.
    """
    from rtl_buddy.runner.test_results import EarlyStopResults

    state = {"live": 0, "peak": 0}
    lock = threading.Lock()

    def occupy(name):
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
        time.sleep(0.05)
        with lock:
            state["live"] -= 1
        return EarlyStopResults(name=f"{name}/results", desc="Stopped at compile")

    stub_runner.group_of = lambda _name: "one-shared-build-dir"
    stub_runner.compile_hook = occupy
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["--machine", "_build-job", "-c", "tests.yaml", "-l", "5", "--parallel", "4"],
    )
    assert result.exit_code == 0, result.output
    assert state["peak"] == 1, "two same-key builds overlapped"
    payload = _build_payload(result.output)
    assert payload["built"] == ["basic", "extra"]

    # One group, so the pool is capped back to it: the budget buys nothing
    # when there is only one thing to build. Both numbers are on the record
    # — `parallel` is what the job used, `parallel_requested` is what the
    # head reserved CPUs for, and the gap is what explains an
    # over-provisioned reservation in the right-sizing report.
    done = [
        record
        for record in _records(minimal_project / "rtl_buddy.log")
        if record.get("event") == "build_job.done"
    ]
    assert [(r["parallel"], r["parallel_requested"], r["groups"]) for r in done] == [
        (1, 4, 1)
    ]


def test_build_job_reports_in_plan_order_when_a_member_fails(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """The envelope is plan-ordered, not group-ordered (#495).

    Three configs in two *interleaved* groups: basic (plan 0) and gamma
    (plan 2) share a compile key, extra (plan 1) has its own. The pool
    yields whole groups, so its natural order is basic, gamma, extra — the
    order the head must read back is basic, extra, gamma, and only the sort
    makes the two agree. basic fails as well, so ``failed`` is pinned
    against the same reordering. Delete the sort and this test says so.
    """
    from rtl_buddy.runner.test_results import EarlyStopResults

    _add_third_test(minimal_project)

    def first_fails(name):
        if name == "basic":
            # Slow *and* failing: completion order would put it last.
            time.sleep(0.1)
            return CompileFailResults(name="basic/results")
        return EarlyStopResults(name=f"{name}/results", desc="Stopped at compile")

    stub_runner.group_of = lambda name: "solo" if name == "extra" else "shared"
    stub_runner.compile_hook = first_fails
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["--machine", "_build-job", "-c", "tests.yaml", "-l", "5", "--parallel", "2"],
    )
    assert result.exit_code == 0, result.output
    payload = _build_payload(result.output)
    assert payload["built"] == ["extra", "gamma"], payload
    assert payload["failed"] == ["basic"]

    done = [
        record
        for record in _records(minimal_project / "rtl_buddy.log")
        if record.get("event") == "build_job.done"
    ]
    assert [r["groups"] for r in done] == [2]


def test_build_job_setup_failure_is_a_failed_test_not_a_failed_job(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """A PRE failure in the serial phase is that config's failure alone.

    It never reaches a builder, so it is reported the way the old serial
    loop reported it — failed, one ``build_job.compile_failed``, exit 0 —
    and the configs behind it still compile.
    """
    from rtl_buddy.runner.test_results import EarlyStopResults, SetupFailResults

    stub_runner.prepare_hook = lambda name: (
        SetupFailResults(name="basic/results", desc="preproc raised")
        if name == "basic"
        else None
    )
    stub_runner.canned = EarlyStopResults(name="b/results", desc="Stopped at compile")
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["--machine", "_build-job", "-c", "tests.yaml", "-l", "5", "--parallel", "2"],
    )
    assert result.exit_code == 0, result.output
    payload = _build_payload(result.output)
    assert payload["built"] == ["extra"]
    assert payload["failed"] == ["basic"]

    records = _records(minimal_project / "rtl_buddy.log")
    assert [
        r["test"] for r in records if r.get("event") == "build_job.compile_failed"
    ] == ["basic"]
    # A setup failure is not a worker crash; it must not be reported as one.
    assert not [
        r for r in records if r.get("event") == "build_job.compile_worker_error"
    ]


def test_build_job_filelist_failure_in_the_probe_is_a_failed_test(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """The probe writes ``run.f``, so it fails the way the compile does.

    A config whose filelist cannot be written has no group dir and never
    joins the pool; it is failed here, and the job still exits 0.
    """
    from rtl_buddy.runner.test_results import EarlyStopResults, FilelistFailResults

    stub_runner.group_fail = lambda name: (
        FilelistFailResults(name="extra/results", desc="no such source")
        if name == "extra"
        else None
    )
    stub_runner.canned = EarlyStopResults(name="b/results", desc="Stopped at compile")
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["--machine", "_build-job", "-c", "tests.yaml", "-l", "5", "--parallel", "2"],
    )
    assert result.exit_code == 0, result.output
    payload = _build_payload(result.output)
    assert payload["built"] == ["basic"]
    assert payload["failed"] == ["extra"]
    assert [
        r["test"]
        for r in _records(minimal_project / "rtl_buddy.log")
        if r.get("event") == "build_job.compile_failed"
    ] == ["extra"]


def test_build_job_serial_phase_exception_is_a_failed_test_not_a_failed_job(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """The exit-0 contract covers the serial phase too (#495).

    The probe pulled the compile-flag assembly ahead of the builder, which
    moved fatals like SystemCSim's missing ``cfg-systemc`` onto the main
    thread. One config's fatal must not cancel the afterok fan-out for the
    ones that were fine.
    """
    from rtl_buddy.runner.test_results import EarlyStopResults

    def boom_for_basic(name):
        if name == "basic":
            raise FatalRtlBuddyError("cfg-systemc missing")
        return None

    stub_runner.prepare_hook = boom_for_basic
    stub_runner.canned = EarlyStopResults(name="b/results", desc="Stopped at compile")
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["--machine", "_build-job", "-c", "tests.yaml", "-l", "5", "--parallel", "2"],
    )
    assert result.exit_code == 0, result.output
    payload = _build_payload(result.output)
    assert payload["built"] == ["extra"]
    assert payload["failed"] == ["basic"]
    assert [
        (r["test"], r["error"])
        for r in _records(minimal_project / "rtl_buddy.log")
        if r.get("event") == "build_job.compile_worker_error"
    ] == [("basic", "cfg-systemc missing")]


def test_build_job_worker_exception_is_a_failed_test_not_a_failed_job(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """An exception inside a compile worker must not escape the job.

    A build job that exits non-zero makes Slurm cancel every afterok sim
    job behind it — the whole point of the best-effort contract. A crashed
    config also says nothing about the others, so they still compile.
    """
    from rtl_buddy.runner.test_results import EarlyStopResults

    def boom_for_basic(name):
        if name == "basic":
            raise RuntimeError("builder vanished")
        return EarlyStopResults(name=f"{name}/results", desc="Stopped at compile")

    stub_runner.compile_hook = boom_for_basic
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["--machine", "_build-job", "-c", "tests.yaml", "-l", "5", "--parallel", "2"],
    )
    assert result.exit_code == 0, result.output
    payload = _build_payload(result.output)
    assert payload["built"] == ["extra"]
    assert payload["failed"] == ["basic"]

    errors = [
        record
        for record in _records(minimal_project / "rtl_buddy.log")
        if record.get("event") == "build_job.compile_worker_error"
    ]
    assert [(r["test"], r["error"]) for r in errors] == [("basic", "builder vanished")]


def test_worker_error_warning_has_a_dedicated_human_message():
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(
        "build_job.compile_worker_error", {"test": "basic", "error": "builder vanished"}
    )
    assert "basic" in msg and "builder vanished" in msg
    assert "exits 0" in msg
    assert "build_job compile_worker_error" not in msg


def test_pool_configured_has_a_dedicated_human_message():
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message("build_job.pool_configured", {"groups": 8, "parallel": 4})
    assert "8 distinct build(s)" in msg
    assert "4 at a time" in msg


def test_build_job_compile_failure_is_best_effort_exit_0(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    # A per-test compile failure must not fail the build job (afterok
    # dependents still run; the failing test recompiles in its own sim job).
    stub_runner.canned = CompileFailResults(name="b/results")
    runner, rb = _runner()
    result = runner.invoke(rb.app, ["--machine", "_build-job", "-c", "tests.yaml"])
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    envelope = json.loads(payload_line)
    assert "basic" in envelope["payload"]["failed"]
    assert envelope["payload"]["built"] == []


def test_build_job_exits_0_when_git_is_missing(
    minimal_project: Path,
    stub_runner: type[_StubTestRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """A node without a ``git`` binary must not cost a regression its fan-out.

    ECP CI, 2026-08-19: the compiles all succeeded, then the machine-result
    envelope shelled out to git, which the compute node did not have. The
    FileNotFoundError propagated, the build job exited non-zero, and Slurm
    cancelled every afterok sim job behind it — ~150 per build, on every branch.
    """
    from rtl_buddy.runner.test_results import EarlyStopResults

    stub_runner.canned = EarlyStopResults(name="b/results", desc="Stopped at compile")

    real_run = subprocess.run

    def git_is_not_installed(argv, *args, **kwargs):
        if argv and argv[0] == "git":
            raise FileNotFoundError(2, "No such file or directory", "git")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(rtl_buddy_module.subprocess, "run", git_is_not_installed)

    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["--machine", "_build-job", "-c", "tests.yaml", "-l", "5"]
    )
    assert result.exit_code == 0, result.output

    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    envelope = json.loads(payload_line)
    # The envelope still parses; the git block degrades to null rather than
    # taking the job down with it.
    assert envelope["meta"]["git"] is None
    assert set(envelope["payload"]["built"]) == {"basic", "extra"}


def test_build_job_exits_0_when_the_envelope_cannot_be_emitted(
    minimal_project: Path,
    stub_runner: type[_StubTestRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """Reporting is never allowed to decide the build job's exit status."""
    from rtl_buddy.runner.test_results import EarlyStopResults

    stub_runner.canned = EarlyStopResults(name="b/results", desc="Stopped at compile")

    def boom(self, *args, **kwargs):
        raise RuntimeError("no envelope for you")

    monkeypatch.setattr(RtlBuddy, "_emit_machine_result", boom)

    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["--machine", "_build-job", "-c", "tests.yaml", "-l", "5"]
    )
    assert result.exit_code == 0, result.output


def test_envelope_failure_warning_has_a_dedicated_human_message():
    """A WARNING must not fall through to the generic event-name fallback."""
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(
        "build_job.machine_result_failed", {"error": "no envelope for you"}
    )
    assert "machine-result envelope" in msg
    assert "no envelope for you" in msg
    assert "exits 0" in msg
    assert "build_job machine_result_failed" not in msg


def test_build_job_plan_compiles_plan_configs_without_hook(
    minimal_project: Path,
    stub_runner: type[_StubTestRunner],
    monkeypatch: pytest.MonkeyPatch,
):
    """--plan makes the build job compile the head's configs and never
    re-run the suite's sweep expansion."""
    from rtl_buddy.dispatch.plan import write_plan
    from rtl_buddy.runner.result_io import load_build_result_json
    from rtl_buddy.runner.test_results import EarlyStopResults

    plan = write_plan(
        minimal_project / "plan.json",
        "tests.yaml",
        SuiteConfig(path="tests.yaml").get_tests(),
        "tok",
    )

    def boom(*a, **k):  # the expansion path must not be taken under --plan
        raise AssertionError("build job must not expand sweeps when --plan is given")

    stub_runner.canned = EarlyStopResults(name="b/results", desc="compiled")
    runner, rb = _runner()
    monkeypatch.setattr(rb, "_iter_suite_runnables", boom)

    result = runner.invoke(
        rb.app,
        [
            "--machine",
            "_build-job",
            "-c",
            "tests.yaml",
            "--plan",
            str(plan),
            "--result-json",
            "br.json",
        ],
    )
    assert result.exit_code == 0, result.output
    # The build result file the head reads for compile-fail parity.
    br = load_build_result_json(minimal_project / "br.json")
    assert set(br["built"]) == {"basic", "extra"}
    assert br["failed"] == []


# ------------------------------------- build telemetry (#495)


def test_build_envelope_carries_per_compile_records(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """`builds` records what each config's compile cost (#495).

    Plan order, one row per planned config, carrying the duration/builder/
    reused triple the sim's own instance observed plus the group directory
    it compiled into — which is what makes two configs that shared one
    build readable as such.
    """
    from rtl_buddy.runner.result_io import load_build_result_json
    from rtl_buddy.runner.test_results import EarlyStopResults

    stub_runner.canned = EarlyStopResults(name="b/results", desc="compiled")
    stub_runner.group_of = lambda name: "/w/.shared-builds/obj_dir_cafe"
    stub_runner.compile_record_of = lambda name: {
        "duration_sec": 12.5 if name == "basic" else 0.0,
        "builder": "stub-builder",
        "reused": name != "basic",
    }
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["_build-job", "-c", "tests.yaml", "-l", "5", "--result-json", "b.json"]
    )
    assert result.exit_code == 0, result.output

    br = load_build_result_json(minimal_project / "b.json")
    assert [entry["test"] for entry in br["builds"]] == ["basic", "extra"]
    assert br["builds"][0] == {
        "test": "basic",
        "builder": "stub-builder",
        "duration_sec": 12.5,
        "reused": False,
        # The basename, never the compute node's absolute path.
        "group": "obj_dir_cafe",
    }
    assert br["builds"][1]["reused"] is True
    assert br["builds"][1]["duration_sec"] == 0.0


def test_build_records_name_the_builder_even_when_the_compile_never_ran(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """A failed config still gets a row — a gap means "never seen" (#495).

    A missing record must not read as "compiled instantly": every planned
    config appears, with the fields it could not know left null. The
    builder is not one of those: it is settled the moment the sim exists,
    which is before the PRE that failed, so the row still names it.
    """
    from rtl_buddy.runner.result_io import load_build_result_json
    from rtl_buddy.runner.test_results import EarlyStopResults, SetupFailResults

    stub_runner.canned = EarlyStopResults(name="b/results", desc="compiled")
    stub_runner.prepare_hook = lambda name: (
        SetupFailResults(name=name, desc="preproc blew up") if name == "basic" else None
    )
    stub_runner.compile_record_of = lambda name: (
        None
        if name == "basic"
        else {"duration_sec": 3.0, "builder": "stub-builder", "reused": False}
    )
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["_build-job", "-c", "tests.yaml", "-l", "5", "--result-json", "b.json"]
    )
    assert result.exit_code == 0, result.output

    br = load_build_result_json(minimal_project / "b.json")
    assert br["failed"] == ["basic"]
    by_test = {entry["test"]: entry for entry in br["builds"]}
    assert by_test["basic"]["duration_sec"] is None
    assert by_test["basic"]["reused"] is None
    assert by_test["basic"]["builder"] == "stub-builder"
    assert by_test["extra"]["duration_sec"] == 3.0


def test_build_records_that_cannot_be_serialised_do_not_cost_the_envelope(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """Telemetry never takes built/failed down with it (#495).

    `built`/`failed` is the load-bearing half — it is what maps a compile
    failure to a CompileFail row. An unserialisable compile record drops
    the telemetry and keeps the envelope, and the job still exits 0 so its
    afterok dependents run.
    """
    from rtl_buddy.runner.result_io import load_build_result_json
    from rtl_buddy.runner.test_results import EarlyStopResults

    stub_runner.canned = EarlyStopResults(name="b/results", desc="compiled")
    stub_runner.compile_record_of = lambda name: {
        "duration_sec": object(),  # not JSON
        "builder": "stub-builder",
        "reused": False,
    }
    runner, rb = _runner()
    result = runner.invoke(
        rb.app, ["_build-job", "-c", "tests.yaml", "-l", "5", "--result-json", "b.json"]
    )
    assert result.exit_code == 0, result.output

    br = load_build_result_json(minimal_project / "b.json")
    assert set(br["built"]) == {"basic", "extra"}
    assert br["builds"] == []


# ------------------------------------- job log paths (#437)


def _records(log_path: Path) -> list[dict]:
    """Every record in a machine-mode rtl_buddy log, fields included.

    ``log_event`` fields are flattened into the JSON line beside ``event``
    (JsonLinesFormatter), so a record is the assertion surface for both the
    event name and what it carried.
    """
    return [
        json.loads(line) for line in log_path.read_text().splitlines() if line.strip()
    ]


def _events(log_path: Path) -> list[str]:
    """Event names in a machine-mode rtl_buddy log."""
    return [record.get("event") for record in _records(log_path)]


def test_test_job_logs_beside_its_envelope_and_never_the_suite_log(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """The head owns ``<suite>/rtl_buddy.log``; a job must not open it.

    ``attach_file_log`` truncates on a process's first open of a path, so
    a job that attached there would erase the head's records for that
    suite (#437). The sentinel content below is the head's; it must come
    back byte-identical.
    """
    stub_runner.canned = TestPassResults(name="basic/results")
    suite_log = minimal_project / "rtl_buddy.log"
    suite_log.write_bytes(b"head-only record\n")
    before = suite_log.read_bytes()

    result_json = (
        minimal_project / "artefacts" / "basic" / "dispatch" / "result-0001.json"
    )
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "--machine",
            "_test-job",
            "basic",
            "--result-json",
            str(result_json),
            "--run-id",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output

    job_log = job_log_path(result_json)
    assert job_log == result_json.parent / "rtl_buddy-0001.log"
    assert "command.test_job" in _events(job_log)
    assert suite_log.read_bytes() == before, (
        "the job rewrote the head's suite log — this is the #437 bug"
    )


def test_build_job_logs_beside_its_envelope_and_never_the_suite_log(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    from rtl_buddy.runner.test_results import EarlyStopResults

    stub_runner.canned = EarlyStopResults(name="b/results", desc="compiled")
    suite_log = minimal_project / "rtl_buddy.log"
    suite_log.write_bytes(b"head-only record\n")
    before = suite_log.read_bytes()

    result_json = minimal_project / "artefacts" / ".dispatch" / "build-result-4711.json"
    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "--machine",
            "_build-job",
            "-c",
            "tests.yaml",
            "--result-json",
            str(result_json),
        ],
    )
    assert result.exit_code == 0, result.output

    job_log = job_log_path(result_json)
    assert job_log == result_json.parent / "build-rtl_buddy-4711.log"
    assert "command.build_job" in _events(job_log)
    assert suite_log.read_bytes() == before


def test_build_job_without_result_json_falls_back_to_the_suite_log(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """Run by hand there is no envelope to pair with and no head to
    collide with, so the suite log is still the right place."""
    from rtl_buddy.runner.test_results import EarlyStopResults

    stub_runner.canned = EarlyStopResults(name="b/results", desc="compiled")
    suite_log = minimal_project / "rtl_buddy.log"
    assert not suite_log.exists()

    runner, rb = _runner()
    result = runner.invoke(rb.app, ["--machine", "_build-job", "-c", "tests.yaml"])
    assert result.exit_code == 0, result.output
    assert "command.build_job" in _events(suite_log)
