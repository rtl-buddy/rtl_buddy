"""Contract tests for the rb xplr P1 command surface.

register / attach-outcome / list / show, exercised the way an agent
drives them: machine-mode JSON envelopes on stdout, JSON manifests in
via ``--json <file|->``. Error paths must exit 2 with a message naming
exactly what was wrong, and (in machine mode) still emit an envelope.

Commands run through ``RtlBuddy.run()`` with a patched ``sys.argv`` —
the same entry point real agents hit — because the FatalRtlBuddyError
-> exit-2 -> machine-envelope contract lives in ``run()``, which
``CliRunner.invoke`` bypasses.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.xplr.schema import validate_record


XPLR_FIXTURES = Path(__file__).parent / "fixtures" / "xplr"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    *,
    stdin: str | None = None,
) -> tuple[int, str, str]:
    """Run one rb invocation through RtlBuddy.run(); return (code, out, err).

    Locks are released after each run: ArtifactLocks holds its flock
    fds until process exit, and a second fd on the same lock file
    conflicts even within one process — so back-to-back commands in one
    test would deadlock-fail without this.
    """
    rb = RtlBuddy(name="test_xplr_cli")
    monkeypatch.setattr(sys, "argv", ["rb", *argv])
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    try:
        code = rb.run()
    finally:
        rb._artifact_locks.release_all()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _envelope(out: str) -> dict:
    payload = json.loads(out)
    assert {"command", "exit_code", "meta", "payload"} <= set(payload)
    assert "rtl_buddy_version" in payload["meta"]
    return payload


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=rb@test.invalid", "-c", "user.name=rb", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_project(minimal_project: Path) -> Path:
    """minimal_project turned into a clean git repo.

    artefacts/ and rtl_buddy.log are gitignored — as any real rb
    project must — so command side effects don't flip the dirty bit.
    """
    (minimal_project / ".gitignore").write_text("artefacts/\nrtl_buddy.log\n")
    _git(minimal_project, "init", "-q", "-b", "main", ".")
    _git(minimal_project, "add", "-A")
    _git(minimal_project, "commit", "-q", "-m", "init")
    return minimal_project


def _head_sha(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


def _manifest_path(project: Path, doc: dict, name: str = "manifest.json") -> Path:
    """Write a JSON input doc *outside* the repo so the tree stays clean."""
    path = project.parent / name
    path.write_text(json.dumps(doc))
    return path


_KNOBS = [
    {
        "name": "synth.target_freq_mhz",
        "from": 500,
        "to": 600,
        "rationale": "probe timing slack headroom",
        "layer": "flow",
    },
    {"name": "rtl.PIPELINE_DEPTH", "from": 2, "to": 3, "layer": "source"},
]


def _register(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    doc: dict | None = None,
) -> dict:
    """Register an experiment via stdin JSON; return the machine payload."""
    doc = doc if doc is not None else {"knobs": _KNOBS}
    code, out, _ = _run(
        ["--machine", "xplr", "register", "--json", "-"],
        monkeypatch,
        capsys,
        stdin=json.dumps(doc),
    )
    assert code == 0, out
    return _envelope(out)["payload"]


# ---------------------------------------------------------------------------
# help text
# ---------------------------------------------------------------------------


def test_xplr_help_lists_subcommands():
    import re

    from typer.testing import CliRunner

    # CI terminals (GitHub Actions) get rich help with ANSI styling that
    # splits option tokens; strip escapes before substring asserts.
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    rb = RtlBuddy(name="test_xplr_help")
    result = CliRunner().invoke(rb.app, ["xplr", "--help"])
    assert result.exit_code == 0
    output = ansi.sub("", result.output)
    for sub in ("register", "attach-outcome", "list", "show"):
        assert sub in output
    result = CliRunner().invoke(rb.app, ["xplr", "register", "--help"])
    assert result.exit_code == 0
    assert "--json" in ansi.sub("", result.output)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def test_register_pins_head_and_emits_full_record(
    git_project: Path, monkeypatch, capsys
):
    manifest = _manifest_path(
        git_project,
        {
            "knobs": _KNOBS,
            "hypothesis": "faster clock still meets timing",
            "provenance": {"agent": "test-agent"},
        },
    )
    code, out, _ = _run(
        ["--machine", "xplr", "register", "--json", str(manifest)],
        monkeypatch,
        capsys,
    )
    assert code == 0, out

    envelope = _envelope(out)
    assert envelope["command"] == "xplr register"
    assert envelope["exit_code"] == 0
    payload = envelope["payload"]
    assert payload["id"] == "exp-0001"

    record = payload["record"]
    validate_record(record)
    assert record["source"]["git_sha"] == _head_sha(git_project)
    assert record["source"]["branch"] == "main"
    assert record["source"]["dirty"] is False
    assert record["knobs"] == _KNOBS
    assert record["hypothesis"] == "faster clock still meets timing"
    assert record["outcome"] == {"status": "pending"}
    assert record["provenance"]["agent"] == "test-agent"
    created = datetime.fromisoformat(record["provenance"]["created"])
    assert created.tzinfo is not None  # RFC 3339 offset required

    # the envelope record is exactly what landed on disk
    record_path = Path(payload["record_path"])
    assert record_path == git_project / "artefacts" / "xplr" / "exp-0001" / (
        "record.json"
    )
    assert json.loads(record_path.read_text()) == record


def test_register_ids_increment(git_project: Path, monkeypatch, capsys):
    assert _register(git_project, monkeypatch, capsys)["id"] == "exp-0001"
    assert _register(git_project, monkeypatch, capsys)["id"] == "exp-0002"


def test_register_without_json_opens_baseline_experiment(
    git_project: Path, monkeypatch, capsys
):
    code, out, _ = _run(["--machine", "xplr", "register"], monkeypatch, capsys)
    assert code == 0, out
    record = _envelope(out)["payload"]["record"]
    validate_record(record)
    assert record["knobs"] == []
    assert record["outcome"]["status"] == "pending"


def test_register_snapshots_dirty_tree_to_exp_branch(
    git_project: Path, monkeypatch, capsys
):
    """P2 commit policy (auto, the default): a dirty tree is snapshotted
    to an exp/<id> branch so the pin is exact — never recorded dirty.
    The full policy matrix lives in test_xplr_gitprov.py."""
    head_before = _head_sha(git_project)
    (git_project / "tests.yaml").write_text("# mutated tracked file\n")
    record = _register(git_project, monkeypatch, capsys)["record"]
    assert record["source"]["dirty"] is False
    assert record["source"]["branch"] == "exp/exp-0001"
    assert record["source"]["diff_from"] == head_before
    assert record["source"]["git_sha"] == _git(git_project, "rev-parse", "exp/exp-0001")
    # the user's checkout is untouched: still on main at the old HEAD,
    # the mutation still uncommitted
    assert _head_sha(git_project) == head_before
    assert "tests.yaml" in _git(git_project, "status", "--porcelain")


def test_register_with_declared_sha_needs_no_git(
    minimal_project: Path, monkeypatch, capsys
):
    doc = {
        "knobs": [],
        "source": {"git_sha": "a1b2c3d", "branch": "main", "diff_from": "0fe1dca"},
    }
    record = _register(minimal_project, monkeypatch, capsys, doc)["record"]
    assert record["source"] == doc["source"]
    assert "dirty" not in record["source"]  # unknowable for a declared sha


def test_register_without_git_or_sha_exits_2(
    minimal_project: Path, monkeypatch, capsys
):
    code, out, err = _run(
        ["--machine", "xplr", "register", "--json", "-"],
        monkeypatch,
        capsys,
        stdin="{}",
    )
    assert code == 2
    envelope = _envelope(out)
    assert envelope["exit_code"] == 2
    assert "source.git_sha" in envelope["payload"]["error"]
    assert "source.git_sha" in err


def test_register_unknown_key_exits_2(git_project: Path, monkeypatch, capsys):
    code, out, _ = _run(
        ["--machine", "xplr", "register", "--json", "-"],
        monkeypatch,
        capsys,
        stdin=json.dumps({"knobs": [], "outcome": {"status": "success"}}),
    )
    assert code == 2
    error = _envelope(out)["payload"]["error"]
    assert "'outcome'" in error
    assert "allowed keys" in error


def test_register_schema_invalid_knob_exits_2(git_project: Path, monkeypatch, capsys):
    code, out, _ = _run(
        ["--machine", "xplr", "register", "--json", "-"],
        monkeypatch,
        capsys,
        stdin=json.dumps({"knobs": [{"name": "k", "from": 1}]}),  # missing "to"
    )
    assert code == 2
    error = _envelope(out)["payload"]["error"]
    assert "/knobs/0" in error
    assert "'to'" in error
    # nothing was written
    assert not (git_project / "artefacts" / "xplr" / "exp-0001").exists()


def test_register_malformed_json_exits_2(git_project: Path, monkeypatch, capsys):
    code, out, _ = _run(
        ["--machine", "xplr", "register", "--json", "-"],
        monkeypatch,
        capsys,
        stdin="not json {",
    )
    assert code == 2
    assert "not valid JSON" in _envelope(out)["payload"]["error"]


def test_register_missing_json_file_exits_2(git_project: Path, monkeypatch, capsys):
    code, out, _ = _run(
        ["--machine", "xplr", "register", "--json", "no-such-file.json"],
        monkeypatch,
        capsys,
    )
    assert code == 2
    assert "no-such-file.json" in _envelope(out)["payload"]["error"]


def test_register_human_mode_reports_id(git_project: Path, monkeypatch, capsys):
    code, _, err = _run(
        ["xplr", "register", "--json", "-"],
        monkeypatch,
        capsys,
        stdin=json.dumps({"knobs": _KNOBS}),
    )
    assert code == 0
    assert "exp-0001" in err  # human messages ride on stderr


# ---------------------------------------------------------------------------
# attach-outcome
# ---------------------------------------------------------------------------


_OUTCOME = {
    "status": "success",
    "metrics": {"wns_ns": -0.12, "routed": True},
    "metric_meta": {"wns_ns": {"direction": "max", "unit": "ns"}},
    "artifacts": ["synth.log"],
    "provenance": {
        "tools": [{"name": "yosys", "version": "0.38"}],
        "reused_state": "synth",
    },
}


def _attach(
    monkeypatch,
    capsys,
    exp_id: str = "exp-0001",
    doc: dict | None = None,
    *,
    force: bool = False,
) -> tuple[int, str]:
    argv = ["--machine", "xplr", "attach-outcome", exp_id, "--json", "-"]
    if force:
        argv.append("--force")
    code, out, _ = _run(
        argv, monkeypatch, capsys, stdin=json.dumps(doc if doc else _OUTCOME)
    )
    return code, out


def test_attach_outcome_roundtrip(git_project: Path, monkeypatch, capsys):
    registered = _register(git_project, monkeypatch, capsys)["record"]

    code, out = _attach(monkeypatch, capsys)
    assert code == 0, out
    envelope = _envelope(out)
    assert envelope["command"] == "xplr attach-outcome"
    record = envelope["payload"]["record"]
    validate_record(record)

    assert record["outcome"] == {
        "status": "success",
        "metrics": {"wns_ns": -0.12, "routed": True},
        "metric_meta": {"wns_ns": {"direction": "max", "unit": "ns"}},
        "artifacts": ["synth.log"],
    }
    # provenance merged, registration fields preserved
    assert record["provenance"]["created"] == registered["provenance"]["created"]
    assert {"name": "yosys", "version": "0.38"} in record["provenance"]["tools"]
    assert record["provenance"]["reused_state"] == "synth"
    # knob manifest untouched
    assert record["knobs"] == registered["knobs"]
    # persisted
    on_disk = json.loads(Path(envelope["payload"]["record_path"]).read_text())
    assert on_disk == record


def test_attach_outcome_double_attach_rejected_unless_forced(
    git_project: Path, monkeypatch, capsys
):
    _register(git_project, monkeypatch, capsys)
    code, _ = _attach(monkeypatch, capsys)
    assert code == 0

    code, out = _attach(monkeypatch, capsys, doc={"status": "failed"})
    assert code == 2
    error = _envelope(out)["payload"]["error"]
    assert "terminal outcome" in error
    assert "--force" in error

    code, out = _attach(monkeypatch, capsys, doc={"status": "failed"}, force=True)
    assert code == 0
    assert _envelope(out)["payload"]["record"]["outcome"] == {"status": "failed"}


def test_attach_outcome_unknown_experiment_exits_2(
    git_project: Path, monkeypatch, capsys
):
    _register(git_project, monkeypatch, capsys)
    code, out = _attach(monkeypatch, capsys, exp_id="exp-0042")
    assert code == 2
    error = _envelope(out)["payload"]["error"]
    assert "unknown experiment id 'exp-0042'" in error
    assert "exp-0001" in error  # tells the agent what does exist


def test_attach_outcome_requires_terminal_status(
    git_project: Path, monkeypatch, capsys
):
    _register(git_project, monkeypatch, capsys)
    code, out = _attach(monkeypatch, capsys, doc={"status": "running"})
    assert code == 2
    error = _envelope(out)["payload"]["error"]
    assert "'success'" in error and "'failed'" in error


def test_attach_outcome_schema_invalid_metrics_exits_2(
    git_project: Path, monkeypatch, capsys
):
    _register(git_project, monkeypatch, capsys)
    code, out = _attach(
        monkeypatch,
        capsys,
        doc={"status": "success", "metrics": {"wns": "fast"}},  # not number/bool
    )
    assert code == 2
    assert "/outcome/metrics" in _envelope(out)["payload"]["error"]
    # the pending record was not clobbered
    on_disk = json.loads(
        (git_project / "artefacts" / "xplr" / "exp-0001" / "record.json").read_text()
    )
    assert on_disk["outcome"] == {"status": "pending"}


# ---------------------------------------------------------------------------
# list / show
# ---------------------------------------------------------------------------


def test_list_summaries_and_status_filter(git_project: Path, monkeypatch, capsys):
    _register(git_project, monkeypatch, capsys, {"knobs": _KNOBS, "hypothesis": "h1"})
    _register(git_project, monkeypatch, capsys, {"knobs": []})
    _attach(monkeypatch, capsys, "exp-0001")

    code, out, _ = _run(["--machine", "xplr", "list"], monkeypatch, capsys)
    assert code == 0
    experiments = _envelope(out)["payload"]["experiments"]
    assert [e["id"] for e in experiments] == ["exp-0001", "exp-0002"]
    first = experiments[0]
    assert first["status"] == "success"
    assert first["git_sha"] == _head_sha(git_project)
    assert first["n_knobs"] == len(_KNOBS)
    assert first["hypothesis"] == "h1"
    assert "created" in first
    assert "hypothesis" not in experiments[1]  # absent stays absent

    code, out, _ = _run(
        ["--machine", "xplr", "list", "--status", "pending"], monkeypatch, capsys
    )
    assert code == 0
    assert [e["id"] for e in _envelope(out)["payload"]["experiments"]] == ["exp-0002"]

    code, out, _ = _run(
        ["--machine", "xplr", "list", "--status", "bogus"], monkeypatch, capsys
    )
    assert code == 2
    assert "bogus" in _envelope(out)["payload"]["error"]


def test_list_empty_ledger(minimal_project: Path, monkeypatch, capsys):
    code, out, _ = _run(["--machine", "xplr", "list"], monkeypatch, capsys)
    assert code == 0
    assert _envelope(out)["payload"]["experiments"] == []


def test_list_human_mode_renders_table(git_project: Path, monkeypatch, capsys):
    _register(git_project, monkeypatch, capsys)
    code, out, err = _run(["xplr", "list"], monkeypatch, capsys)
    assert code == 0
    assert "exp-0001" in out + err


def test_show_machine_and_human(git_project: Path, monkeypatch, capsys):
    _register(git_project, monkeypatch, capsys)
    on_disk_text = (
        git_project / "artefacts" / "xplr" / "exp-0001" / "record.json"
    ).read_text()

    code, out, _ = _run(["--machine", "xplr", "show", "exp-0001"], monkeypatch, capsys)
    assert code == 0
    envelope = _envelope(out)
    assert envelope["command"] == "xplr show"
    assert envelope["payload"]["record"] == json.loads(on_disk_text)

    code, out, _ = _run(["xplr", "show", "exp-0001"], monkeypatch, capsys)
    assert code == 0
    assert out == on_disk_text  # human mode prints the canonical record


def test_show_unknown_experiment_exits_2(minimal_project: Path, monkeypatch, capsys):
    code, out, _ = _run(["--machine", "xplr", "show", "exp-0001"], monkeypatch, capsys)
    assert code == 2
    error = _envelope(out)["payload"]["error"]
    assert "unknown experiment id 'exp-0001'" in error
    assert "ledger is empty" in error


# ---------------------------------------------------------------------------
# --root: anchoring project-root discovery from an unrelated cwd
# ---------------------------------------------------------------------------


def test_xplr_root_option_works_from_unrelated_cwd(
    git_project: Path, monkeypatch, capsys, tmp_path: Path
):
    _register(git_project, monkeypatch, capsys)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    code, out, _ = _run(
        ["--machine", "xplr", "--root", str(git_project), "list"],
        monkeypatch,
        capsys,
    )
    assert code == 0, out
    envelope = _envelope(out)
    assert envelope["command"] == "xplr list"
    assert [e["id"] for e in envelope["payload"]["experiments"]] == ["exp-0001"]
    # the ledger stayed under the --root project, nothing landed in cwd
    assert not (elsewhere / "artefacts").exists()


def test_xplr_root_option_bad_root_exits_2(
    minimal_project: Path, monkeypatch, capsys, tmp_path: Path
):
    no_project = tmp_path / "no_project"
    no_project.mkdir()
    monkeypatch.chdir(no_project)

    # a directory with no root_config.yaml/.git above it: clear exit-2
    code, out, _ = _run(
        ["--machine", "xplr", "--root", str(no_project), "list"],
        monkeypatch,
        capsys,
    )
    assert code == 2
    error = _envelope(out)["payload"]["error"]
    assert "cannot locate project root" in error

    # a path that is not a directory at all
    code, out, _ = _run(
        ["--machine", "xplr", "--root", str(no_project / "nope"), "list"],
        monkeypatch,
        capsys,
    )
    assert code == 2
    assert "not a directory" in _envelope(out)["payload"]["error"]


def test_xplr_outside_project_error_mentions_root_flag(
    monkeypatch, capsys, tmp_path: Path
):
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    monkeypatch.chdir(nowhere)
    code, out, _ = _run(["--machine", "xplr", "list"], monkeypatch, capsys)
    assert code == 2
    assert "--root" in _envelope(out)["payload"]["error"]


# ---------------------------------------------------------------------------
# fixture records (the P0 contract corpus) read back through the CLI
# ---------------------------------------------------------------------------


def test_fixture_records_readable_via_list_and_show(
    minimal_project: Path, monkeypatch, capsys
):
    ledger_root = minimal_project / "artefacts" / "xplr"
    fixtures = sorted((XPLR_FIXTURES / "valid").glob("*.json"))
    for fixture in fixtures:
        target = ledger_root / fixture.stem / "record.json"
        target.parent.mkdir(parents=True)
        shutil.copy(fixture, target)

    code, out, _ = _run(["--machine", "xplr", "list"], monkeypatch, capsys)
    assert code == 0
    experiments = _envelope(out)["payload"]["experiments"]
    assert [e["id"] for e in experiments] == [f.stem for f in fixtures]

    code, out, _ = _run(["--machine", "xplr", "show", "exp-0007"], monkeypatch, capsys)
    assert code == 0
    record = _envelope(out)["payload"]["record"]
    assert record == json.loads((XPLR_FIXTURES / "valid" / "exp-0007.json").read_text())
