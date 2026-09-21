"""One-off ``rb test --plusarg`` runtime plusarg overrides (#552).

The flag adds or replaces a single ``plusargs:`` entry for one invocation,
so a throwaway variation on a configured test — a deliberate-fault pass
reading ``test_cfg.get_plusarg("mutate")``, a bumped ``+timeout_us`` — needs
no edit to a ``tests.yaml`` several agents share.

``TestRunner`` is stubbed out, so these run no simulator: the assertions are
on the ``TestConfig`` handed to it, which is the same object the ``preproc``
hook is exec'd with and the one the simulator command line's plusargs are
built from.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import rtl_buddy.rtl_buddy as rtl_buddy_module
from rtl_buddy.config import SuiteConfig
from rtl_buddy.config.test import parse_plusarg_overrides
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.test_results import TestPassResults


class _StubTestRunner:
    """Stands in for TestRunner: records ctor args, returns a canned PASS."""

    last_init: dict | None = None

    def __init__(self, **kwargs):
        type(self).last_init = kwargs

    def run(self):
        return TestPassResults(name="basic/results")

    def run_multiple(self, run_ids):
        return [self.run() for _ in run_ids]

    @property
    def last_compile(self):
        return None


@pytest.fixture
def stub_runner(monkeypatch: pytest.MonkeyPatch) -> type[_StubTestRunner]:
    _StubTestRunner.last_init = None
    monkeypatch.setattr(rtl_buddy_module, "TestRunner", _StubTestRunner)
    return _StubTestRunner


def _invoke(args) -> tuple[object, RtlBuddy]:
    runner, rb = CliRunner(), RtlBuddy(name="test_plusarg_override")
    return runner.invoke(rb.app, args), rb


def _configure_plusargs(project: Path, yaml_block: str):
    """Give the fixture's ``basic`` test a ``plusargs:`` block of its own."""
    tests_yaml = project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text().replace("    plusargs:\n", yaml_block, 1)
    )


def _ran_with(stub_runner: type[_StubTestRunner]) -> dict | None:
    """The plusargs of the config the stubbed TestRunner was handed."""
    return stub_runner.last_init["test_cfg"].get_plusargs()


# --- the parser ------------------------------------------------------------


def test_the_parser_reads_values_and_valueless_keys():
    assert parse_plusarg_overrides(["mutate=1", "trace", "path=/a/b=c"]) == {
        "mutate": "1",
        "trace": None,
        "path": "/a/b=c",
    }


def test_an_empty_value_is_not_a_valueless_plusarg():
    """``+KEY=`` and ``+KEY`` are different plusargs to a testbench."""
    assert parse_plusarg_overrides(["mutate="]) == {"mutate": ""}
    assert parse_plusarg_overrides(["mutate"]) == {"mutate": None}


def test_no_flag_parses_to_no_overrides():
    assert parse_plusarg_overrides(None) == {}
    assert parse_plusarg_overrides([]) == {}


def test_the_last_repeat_of_one_key_wins():
    assert parse_plusarg_overrides(["mutate=1", "mutate=2"]) == {"mutate": "2"}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("=1", "has no name"),
        ("", "has no name"),
        ("+mutate=1", "must not contain '+'"),
        ("mu tate=1", "must not contain whitespace"),
        ("mutate\t=1", "must not contain whitespace"),
    ],
)
def test_malformed_values_are_rejected(raw, expected):
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        parse_plusarg_overrides([raw])
    assert expected in str(excinfo.value)


def test_a_plus_in_a_value_is_kept():
    """Only the NAME may not carry a ``+``; a value is passed through."""
    assert parse_plusarg_overrides(["opts=a+b"]) == {"opts": "a+b"}


# --- the merge -------------------------------------------------------------


def test_the_merge_leaves_a_config_alone_without_overrides(minimal_project: Path):
    cfg = SuiteConfig(path="tests.yaml").get_tests("basic")[0]
    assert cfg.with_plusarg_overrides({}) is cfg
    assert cfg.with_plusarg_overrides(None) is cfg


def test_the_merge_copies_rather_than_editing_the_loaded_suite(minimal_project: Path):
    _configure_plusargs(minimal_project, "    plusargs:\n      test_cycles: 50\n")
    suite = SuiteConfig(path="tests.yaml")
    cfg = suite.get_tests("basic")[0]
    merged = cfg.with_plusarg_overrides({"test_cycles": "5", "mutate": "1"})
    assert merged is not cfg
    assert merged.get_plusargs() == {"test_cycles": "5", "mutate": "1"}
    # The suite's own config is what tests.yaml says, so a second read of it
    # (the summary's builder line, a re-expansion) is not the merged view.
    assert cfg.get_plusargs() == {"test_cycles": 50}


# --- rb test ---------------------------------------------------------------


def test_a_plusarg_is_added_to_a_test_that_configures_none(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """The negative-control case from the issue: the entry has no plusargs
    at all, and the hook still has to be able to read the one flag."""
    result, _ = _invoke(["test", "basic", "-c", "tests.yaml", "--plusarg", "mutate=1"])
    assert result.exit_code == 0, result.output
    assert _ran_with(stub_runner) == {"mutate": "1"}
    # What the preproc hook actually calls.
    assert stub_runner.last_init["test_cfg"].get_plusarg("mutate") == "1"


def test_a_plusarg_overrides_the_configured_value(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    _configure_plusargs(
        minimal_project, "    plusargs:\n      test_cycles: 50\n      keep: yes\n"
    )
    result, _ = _invoke(
        ["test", "basic", "-c", "tests.yaml", "--plusarg", "test_cycles=5"]
    )
    assert result.exit_code == 0, result.output
    # A CLI value is always a string (argv has no types); an untouched YAML
    # entry keeps whatever YAML made of it — here `yes` as a bool. Both end
    # up interpolated into `+KEY=VALUE` the same way.
    assert _ran_with(stub_runner) == {"test_cycles": "5", "keep": True}


def test_the_last_flag_wins_over_the_earlier_one(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    result, _ = _invoke(
        [
            "test",
            "basic",
            "-c",
            "tests.yaml",
            "--plusarg",
            "mutate=1",
            "--plusarg",
            "mutate=2",
        ]
    )
    assert result.exit_code == 0, result.output
    assert _ran_with(stub_runner) == {"mutate": "2"}


def test_a_bare_key_runs_as_a_valueless_plusarg(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    result, _ = _invoke(["test", "basic", "-c", "tests.yaml", "--plusarg", "trace"])
    assert result.exit_code == 0, result.output
    assert _ran_with(stub_runner) == {"trace": None}


def test_a_run_without_the_flag_keeps_the_configured_plusargs(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """Byte-parity for every existing run: no copy, no added result key."""
    _configure_plusargs(minimal_project, "    plusargs:\n      test_cycles: 50\n")
    result, _ = _invoke(["test", "basic", "-c", "tests.yaml"])
    assert result.exit_code == 0, result.output
    assert _ran_with(stub_runner) == {"test_cycles": 50}
    envelope = json.loads(
        (minimal_project / "artefacts" / "basic" / "result.json").read_text()
    )
    assert "plusarg_overrides" not in envelope["result"]["results"]


def test_a_malformed_plusarg_is_a_usage_error_before_anything_runs(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    result, _ = _invoke(["test", "basic", "-c", "tests.yaml", "--plusarg", "+mutate=1"])
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "must not contain '+'" in str(result.exception)
    assert stub_runner.last_init is None


def test_list_rejects_a_plusarg_it_could_not_apply(minimal_project: Path):
    result, _ = _invoke(["test", "-c", "tests.yaml", "--list", "--plusarg", "mutate=1"])
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "--list cannot be combined with --plusarg" in str(result.exception)


def test_the_result_envelope_records_the_overrides(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """A durable run has to be distinguishable from the yaml entry it
    otherwise looks exactly like — the overrides are the only difference."""
    result, _ = _invoke(
        [
            "test",
            "basic",
            "-c",
            "tests.yaml",
            "--plusarg",
            "mutate=1",
            "--plusarg",
            "trace",
        ]
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(
        (minimal_project / "artefacts" / "basic" / "result.json").read_text()
    )
    assert envelope["result"]["results"]["plusarg_overrides"] == {
        "mutate": "1",
        "trace": None,
    }


def test_the_machine_row_records_the_overrides(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    result, _ = _invoke(
        ["--machine", "test", "basic", "-c", "tests.yaml", "--plusarg", "mutate=1"]
    )
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    envelope = json.loads(payload_line)
    rows = envelope["payload"]["results"]
    assert [row["plusarg_overrides"] for row in rows] == [{"mutate": "1"}]


def test_the_machine_row_omits_the_key_without_the_flag(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    result, _ = _invoke(["--machine", "test", "basic", "-c", "tests.yaml"])
    assert result.exit_code == 0, result.output
    payload_line = [
        line for line in result.output.splitlines() if line.startswith("{")
    ][-1]
    rows = json.loads(payload_line)["payload"]["results"]
    assert all("plusarg_overrides" not in row for row in rows)


def test_an_override_applies_to_every_selected_test(
    minimal_project: Path, monkeypatch: pytest.MonkeyPatch
):
    """The flag is per invocation, not per test: a whole-suite negative
    control run is the point."""
    seen = {}

    class _Recording(_StubTestRunner):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            seen[kwargs["test_cfg"].get_name()] = kwargs["test_cfg"].get_plusargs()

    monkeypatch.setattr(rtl_buddy_module, "TestRunner", _Recording)
    result, _ = _invoke(["test", "-c", "tests.yaml", "--plusarg", "mutate=1"])
    assert result.exit_code == 0, result.output
    assert seen == {"basic": {"mutate": "1"}, "extra": {"mutate": "1"}}


def test_the_summary_footer_names_the_overrides(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    """A human reading the table has to be able to tell it apart from a run
    of the configured entry, the way `Master Seed:` already does (#552)."""
    result, _ = _invoke(
        [
            "test",
            "basic",
            "-c",
            "tests.yaml",
            "--plusarg",
            "mutate=1",
            "--plusarg",
            "trace",
        ]
    )
    assert result.exit_code == 0, result.output
    assert "Plusarg Overrides: +mutate=1 +trace" in result.output


def test_the_summary_footer_is_unchanged_without_the_flag(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    result, _ = _invoke(["test", "basic", "-c", "tests.yaml"])
    assert result.exit_code == 0, result.output
    assert "Plusarg Overrides" not in result.output


def test_overriding_the_managed_seed_plusarg_is_refused(minimal_project: Path):
    """rtl_buddy restores that plusarg from the resolved seed after `preproc`,
    so merging an override into it would drop it silently (#552)."""
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text().replace(
            "  - name: basic\n", "  - name: basic\n    sim-rand-seed-plusarg: stim\n", 1
        )
    )
    cfg = SuiteConfig(path="tests.yaml").get_tests("basic")[0]
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        cfg.with_plusarg_overrides({"stim": "7"})
    assert "sim-rand-seed-plusarg" in str(excinfo.value)
    # An unrelated key is still merged.
    assert cfg.with_plusarg_overrides({"mutate": "1"}).get_plusargs() == {"mutate": "1"}


def test_a_seed_managed_test_refuses_the_override_from_the_cli(
    minimal_project: Path, stub_runner: type[_StubTestRunner]
):
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text().replace(
            "  - name: basic\n", "  - name: basic\n    sim-rand-seed-plusarg: stim\n", 1
        )
    )
    result, _ = _invoke(["test", "basic", "-c", "tests.yaml", "--plusarg", "stim=7"])
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "sim-rand-seed-plusarg" in str(result.exception)
    assert stub_runner.last_init is None
