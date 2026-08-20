"""CLI tests for the hidden ``rb _test-job`` re-entry command (#351 P0).

The command is the unit a remote dispatch backend submits: run one
(test, run_id) and write a ``result.json`` envelope for the collecting
head process. ``TestRunner`` is stubbed out so no real simulator is
needed; the ``minimal_project`` fixture provides the config surface.
"""

from __future__ import annotations

import json
import subprocess
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
    """Stands in for TestRunner: records ctor args, returns a canned result."""

    canned = None
    last_init = None

    def __init__(self, **kwargs):
        type(self).last_init = kwargs

    def run(self):
        return type(self).canned

    def run_multiple(self, run_ids):
        return [type(self).canned for _ in run_ids]


@pytest.fixture
def stub_runner(monkeypatch: pytest.MonkeyPatch) -> type[_StubTestRunner]:
    _StubTestRunner.canned = None
    _StubTestRunner.last_init = None
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


# ------------------------------------- job log paths (#437)


def _events(log_path: Path) -> list[str]:
    """Event names in a machine-mode rtl_buddy log."""
    return [
        json.loads(line).get("event")
        for line in log_path.read_text().splitlines()
        if line.strip()
    ]


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
