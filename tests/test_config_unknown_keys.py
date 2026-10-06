"""Unknown keys in reservation blocks warn instead of vanishing (rtl_buddy#652)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.config import dispatch as dispatch_config
from rtl_buddy.config.model import ModelConfigLoader
from rtl_buddy.config.root import RootConfig
from rtl_buddy.config.suite import SuiteConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.logging_utils import _human_message
from rtl_buddy.rtl_buddy import RtlBuddy


@pytest.fixture(autouse=True)
def _fresh_warning_memo(monkeypatch):
    monkeypatch.setattr(dispatch_config, "_WARNED_UNKNOWN_KEYS", set())


def _unknown_key_events(caplog) -> list[dict]:
    return [
        record.rtl_fields
        for record in caplog.records
        if getattr(record, "rtl_event", None) == "config.unknown_key"
        and record.levelno == logging.WARNING
    ]


def _summary(events) -> set[tuple[str, str, str | None]]:
    return {(e["block"], e["key"], e.get("suggestion")) for e in events}


def test_a_clean_suite_warns_about_nothing(minimal_project: Path, caplog):
    caplog.set_level(logging.WARNING)
    SuiteConfig(path=str(minimal_project / "tests.yaml"))
    assert _unknown_key_events(caplog) == []


def test_suite_reservation_blocks_warn_about_an_unknown_key(
    minimal_project: Path, caplog
):
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        "compile:\n  mem: 48G\n  split_verilate: false\n"
        "  verilate: {cpu: 4}\n"
        + tests_yaml.read_text()
        .replace(
            "  - name: tb_basic\n",
            "  - name: tb_basic\n"
            "    resources: { cpus: 2, memory: 8G }\n"
            '    compile: { mem: 4G, walltime: "01:00:00" }\n',
        )
        .replace(
            "  - name: basic\n",
            '  - name: basic\n    resources: { mem: 8G, tmie: "02:00:00" }\n',
        )
    )
    caplog.set_level(logging.WARNING)
    suite = SuiteConfig(path=str(tests_yaml))

    events = _unknown_key_events(caplog)
    assert _summary(events) == {
        ("compile", "split_verilate", "split-verilate"),
        ("compile.verilate", "cpu", "cpus"),
        ("testbench 'tb_basic' resources", "memory", "mem"),
        ("testbench 'tb_basic' compile", "walltime", "time"),
        ("test 'basic' resources", "tmie", "time"),
    }
    assert {e["path"] for e in events} == {str(tests_yaml)}
    # A warning, not a rejection: the known keys still load and the unknown one stays dropped.
    basic = suite.get_tests("basic")[0]
    assert basic.resources.mem == "8G"
    assert basic.resources.time is None
    assert basic.get_testbench().resources.mem is None
    assert suite.get_compile().split_verilate is None


def test_an_unknown_key_with_no_close_match_has_no_suggestion(
    minimal_project: Path, caplog
):
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text().replace(
            "  - name: tb_basic\n",
            "  - name: tb_basic\n    resources: { partition: verif }\n",
        )
    )
    caplog.set_level(logging.WARNING)
    SuiteConfig(path=str(tests_yaml))
    [event] = _unknown_key_events(caplog)
    assert event["key"] == "partition"
    assert "suggestion" not in event
    assert event["known"] == ["cpus", "mem", "time", "modes"]


def test_a_mode_block_stays_strict(minimal_project: Path, caplog):
    """`modes:` contents are already fatal on an unknown key; the warning does not descend into them."""
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text().replace(
            "  - name: tb_basic\n",
            "  - name: tb_basic\n    resources: { modes: { cov: { memory: 8G } } }\n",
        )
    )
    caplog.set_level(logging.WARNING)
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        SuiteConfig(path=str(tests_yaml))
    assert "unknown key 'memory'" in str(excinfo.value.__cause__)
    assert _unknown_key_events(caplog) == []


def test_the_warning_comes_before_a_fatal_validation(minimal_project: Path, caplog):
    """A misspelt key is named even when the same file then fails to load."""
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        "compile:\n  memory: 48G\n  time: 3:00:00\n" + tests_yaml.read_text()
    )
    caplog.set_level(logging.WARNING)
    with pytest.raises(FatalRtlBuddyError):
        SuiteConfig(path=str(tests_yaml))
    assert _summary(_unknown_key_events(caplog)) == {("compile", "memory", "mem")}


def test_cfg_dispatch_blocks_warn_about_an_unknown_key(minimal_project: Path, caplog):
    root_cfg_path = minimal_project / "root_config.yaml"
    root_cfg_path.write_text(
        root_cfg_path.read_text()
        + "\n".join(
            [
                "\ncfg-dispatch:",
                "  backend: slurm",
                "  poll_interval: 3",
                "  resources: { cpus: 2, memory: 4G }",
                "  compile:",
                "    parallel: 2",
                "    verilate: { mme: 8G }",
                "  coverage: { tme: 01:00:00 }",
                "  retry: { attempt: 2 }",
                "  rightsize: { margins: 2.0 }",
            ]
        )
        + "\n"
    )
    caplog.set_level(logging.WARNING)
    cfg = RootConfig(name="t/root", start_dir=minimal_project).get_dispatch_cfg()

    events = _unknown_key_events(caplog)
    assert _summary(events) == {
        ("cfg-dispatch", "poll_interval", "poll-interval"),
        ("cfg-dispatch.resources", "memory", "mem"),
        ("cfg-dispatch.compile.verilate", "mme", "mem"),
        ("cfg-dispatch.coverage", "tme", "time"),
        ("cfg-dispatch.retry", "attempt", "attempts"),
        ("cfg-dispatch.rightsize", "margins", "margin"),
    }
    assert {e["path"] for e in events} == {str(root_cfg_path)}
    assert cfg.backend == "slurm"
    assert cfg.resources.cpus == 2
    assert cfg.resources.mem is None
    assert cfg.compile.parallel == 2


def test_a_clean_cfg_dispatch_warns_about_nothing(minimal_project: Path, caplog):
    root_cfg_path = minimal_project / "root_config.yaml"
    root_cfg_path.write_text(
        root_cfg_path.read_text()
        + "\ncfg-dispatch:\n  sbatch-args: [--partition=verif]\n"
        "  compile: { split-verilate: false, verilate: { cpus: 2 } }\n"
        "  retry: { backoff-sec: 5 }\n"
    )
    caplog.set_level(logging.WARNING)
    RootConfig(name="t/root", start_dir=minimal_project)
    assert _unknown_key_events(caplog) == []


def test_an_elaboration_resources_block_warns_about_an_unknown_key(
    minimal_project: Path, caplog
):
    models_yaml = minimal_project / "models.yaml"
    models_yaml.write_text(
        models_yaml.read_text()
        + "    elaborations:\n"
        + "      - name: wide\n"
        + "        resources: { mem: 4G, cpu: 2 }\n"
    )
    caplog.set_level(logging.WARNING)
    profile = ModelConfigLoader(str(models_yaml)).get_model("example").elaborations[0]
    assert _summary(_unknown_key_events(caplog)) == {
        ("model 'example' elaboration 'wide' resources", "cpu", "cpus")
    }
    assert profile.resources.cpus is None


def test_a_reloaded_file_warns_once(minimal_project: Path, caplog):
    """A models.yaml is reloaded per test; its warning is not repeated per load."""
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text().replace(
            "  - name: tb_basic\n",
            "  - name: tb_basic\n    resources: { memory: 8G }\n",
        )
    )
    caplog.set_level(logging.WARNING)
    SuiteConfig(path=str(tests_yaml))
    SuiteConfig(path=str(tests_yaml))
    assert len(_unknown_key_events(caplog)) == 1


def test_the_unknown_key_message_names_the_fix():
    message = _human_message(
        "config.unknown_key",
        {
            "path": "tests.yaml",
            "block": "testbench 'tb' resources",
            "key": "memory",
            "suggestion": "mem",
            "known": ["cpus", "mem", "time", "modes"],
        },
    )
    assert message.startswith(
        "tests.yaml: unknown key 'memory' in testbench 'tb' resources ignored"
    )
    assert "did you mean 'mem'?" in message
    assert "known keys are cpus, mem, time, modes" in message
    assert "later major release will make an unknown key fatal" in message


def test_the_warning_reaches_a_default_console(minimal_project: Path):
    """The console handler shows WARNING without -v, so a plain run prints the warning."""
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text().replace(
            "  - name: tb_basic\n",
            "  - name: tb_basic\n    resources: { memory: 8G }\n",
        )
    )
    result = CliRunner().invoke(
        RtlBuddy(name="test_unknown_key").app, ["test", "--list"]
    )
    assert result.exit_code == 0, result.output
    # Rich wraps the console line.
    console = " ".join(result.output.split())
    assert "unknown key 'memory'" in console
    assert "did you mean 'mem'?" in console
