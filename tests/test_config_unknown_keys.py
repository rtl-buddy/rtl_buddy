"""Unknown reservation keys reject the config before defaults can mask a typo."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from rtl_buddy.config.model import ModelConfigLoader
from rtl_buddy.config.root import RootConfig
from rtl_buddy.config.suite import SuiteConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.logging_utils import _human_message


def _unknown_key_events(caplog) -> list[dict]:
    return [
        record.rtl_fields
        for record in caplog.records
        if getattr(record, "rtl_event", None) == "config.unknown_key_rejected"
        and record.levelno == logging.ERROR
    ]


def _summary(events) -> set[tuple[str, str, str | None]]:
    return {(e["block"], e["key"], e.get("suggestion")) for e in events}


def test_a_clean_suite_warns_about_nothing(minimal_project: Path, caplog):
    caplog.set_level(logging.WARNING)
    SuiteConfig(path=str(minimal_project / "tests.yaml"))
    assert _unknown_key_events(caplog) == []


def test_suite_reservation_blocks_reject_all_unknown_keys(
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
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        SuiteConfig(path=str(tests_yaml))

    events = _unknown_key_events(caplog)
    assert _summary(events) == {
        ("compile", "split_verilate", "split-verilate"),
        ("compile.verilate", "cpu", "cpus"),
        ("testbench 'tb_basic' resources", "memory", "mem"),
        ("testbench 'tb_basic' compile", "walltime", "time"),
        ("test 'basic' resources", "tmie", "time"),
    }
    assert {e["path"] for e in events} == {str(tests_yaml)}
    for event in events:
        assert repr(event["key"]) in str(excinfo.value.__cause__)


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
    with pytest.raises(FatalRtlBuddyError):
        SuiteConfig(path=str(tests_yaml))
    [event] = _unknown_key_events(caplog)
    assert event["key"] == "partition"
    assert "suggestion" not in event
    assert event["known"] == ["cpus", "mem", "time", "modes"]


def test_a_mode_block_stays_strict(minimal_project: Path, caplog):
    """Mode contents have their own strict validator."""
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


def test_unknown_keys_are_checked_before_typed_validation(
    minimal_project: Path, caplog
):
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        "compile:\n  memory: 48G\n  time: 3:00:00\n" + tests_yaml.read_text()
    )
    caplog.set_level(logging.WARNING)
    with pytest.raises(FatalRtlBuddyError):
        SuiteConfig(path=str(tests_yaml))
    assert _summary(_unknown_key_events(caplog)) == {("compile", "memory", "mem")}


def test_cfg_dispatch_blocks_reject_all_unknown_keys(minimal_project: Path, caplog):
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
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        RootConfig(name="t/root", start_dir=minimal_project)

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
    for event in events:
        assert repr(event["key"]) in str(excinfo.value.__cause__)


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


def test_an_elaboration_resources_block_rejects_an_unknown_key(
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
    with pytest.raises(FatalRtlBuddyError):
        ModelConfigLoader(str(models_yaml))
    assert _summary(_unknown_key_events(caplog)) == {
        ("model 'example' elaboration 'wide' resources", "cpu", "cpus")
    }


def test_a_reloaded_file_is_rejected_every_time(minimal_project: Path, caplog):
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text().replace(
            "  - name: tb_basic\n",
            "  - name: tb_basic\n    resources: { memory: 8G }\n",
        )
    )
    caplog.set_level(logging.WARNING)
    for _ in range(2):
        with pytest.raises(FatalRtlBuddyError):
            SuiteConfig(path=str(tests_yaml))
    assert len(_unknown_key_events(caplog)) == 2


def test_the_unknown_key_message_names_the_fix():
    message = _human_message(
        "config.unknown_key_rejected",
        {
            "path": "tests.yaml",
            "block": "testbench 'tb' resources",
            "key": "memory",
            "suggestion": "mem",
            "known": ["cpus", "mem", "time", "modes"],
        },
    )
    assert message.startswith(
        "tests.yaml: unknown key 'memory' in testbench 'tb' resources"
    )
    assert "did you mean 'mem'?" in message
    assert "known keys are cpus, mem, time, modes" in message
    assert "ignored" not in message


@pytest.mark.parametrize("machine", [False, True])
def test_invalid_reservations_exit_two_before_running(minimal_project: Path, machine):
    tests_yaml = minimal_project / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text().replace(
            "  - name: tb_basic\n",
            "  - name: tb_basic\n    resources: { memory: 8G }\n",
        )
    )
    result = subprocess.run(
        [sys.executable, "-m", "rtl_buddy"]
        + (["--machine"] if machine else [])
        + ["test", "--list"],
        cwd=minimal_project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    # Rich wraps the console line.
    console = " ".join(result.stderr.split())
    assert "unknown key 'memory'" in console
    assert "did you mean 'mem'?" in console
