"""CLI tests for the hidden ``rb _test-job`` re-entry command (#351 P0).

The command is the unit a remote dispatch backend submits: run one
(test, run_id) and write a ``result.json`` envelope for the collecting
head process. ``TestRunner`` is stubbed out so no real simulator is
needed; the ``minimal_project`` fixture provides the config surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import rtl_buddy.rtl_buddy as rtl_buddy_module
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
