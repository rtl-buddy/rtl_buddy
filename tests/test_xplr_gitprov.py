"""Contract tests for rb xplr P2: git-pinned provenance, worktrees, gc.

Everything runs in throwaway git repos (the minimal_project fixture
turned into one) through ``RtlBuddy.run()`` — the same entry point the
agent hits — so the FatalRtlBuddyError -> exit-2 -> machine-envelope
contract is exercised, not bypassed.

Covered here:

* auto commit-mode: a dirty source scope is snapshotted to an
  ``exp/<id>`` branch containing ONLY the scoped paths, with the
  user's branch/index/working tree untouched (``git status`` before
  == after); a clean tree converges (records HEAD, no branch).
* self-managed commit-mode: a dirty scope is a hard error (exit 2).
* bookkeeping exclusion: the xplr ledger dir and rtl_buddy.log never
  count as source (no snapshot, no dirt, no new sha), identical dirty
  RTL reuses the prior snapshot sha, and register warns when the
  ledger/log are inside the repo but not gitignored.
* ``--baseline`` / parent-derived ``diff_from``.
* materialize/release worktree round trip (idempotent both ways).
* gc: keep-frontier protects frontier members + their direct lineage,
  evicts dominated/failed oldest-first, never touches record.json;
  dry-run evicts nothing; manual policy only lists candidates; the
  register-time hard-cap backstop blocks new runs.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from rtl_buddy.rtl_buddy import RtlBuddy


# ---------------------------------------------------------------------------
# helpers (same conventions as test_xplr_cli.py)
# ---------------------------------------------------------------------------


def _run(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    *,
    stdin: str | None = None,
) -> tuple[int, str, str]:
    rb = RtlBuddy(name="test_xplr_gitprov")
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
    """minimal_project as a clean git repo (artefacts/ gitignored)."""
    (minimal_project / ".gitignore").write_text("artefacts/\nrtl_buddy.log\n")
    _git(minimal_project, "init", "-q", "-b", "main", ".")
    _git(minimal_project, "add", "-A")
    _git(minimal_project, "commit", "-q", "-m", "init")
    return minimal_project


def _set_cfg(project: Path, lines: list[str]) -> None:
    """Append a cfg-xplr block to root_config.yaml and commit it."""
    path = project / "root_config.yaml"
    block = "\ncfg-xplr:\n" + "".join(f"  {line}\n" for line in lines)
    path.write_text(path.read_text() + block)
    _git(project, "add", "root_config.yaml")
    _git(project, "commit", "-q", "-m", "cfg-xplr")


def _register(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    doc: dict | None = None,
    extra_argv: list[str] | None = None,
) -> tuple[int, dict]:
    code, out, _ = _run(
        ["--machine", "xplr", "register", "--json", "-", *(extra_argv or [])],
        monkeypatch,
        capsys,
        stdin=json.dumps(doc if doc is not None else {"knobs": []}),
    )
    return code, _envelope(out)["payload"]


def _attach(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    exp_id: str,
    doc: dict,
) -> None:
    code, out, _ = _run(
        ["--machine", "xplr", "attach-outcome", exp_id, "--json", "-"],
        monkeypatch,
        capsys,
        stdin=json.dumps(doc),
    )
    assert code == 0, out


# ---------------------------------------------------------------------------
# commit policy: auto (default)
# ---------------------------------------------------------------------------


def test_auto_dirty_tree_snapshots_only_scope_and_leaves_tree_alone(
    git_project: Path, monkeypatch, capsys
):
    _set_cfg(git_project, ['source-scope: ["src"]'])
    head = _git(git_project, "rev-parse", "HEAD")

    # dirty inside scope: one tracked modification + one untracked file
    (git_project / "src" / "example.sv").write_text("// modified module\n")
    (git_project / "src" / "new_block.sv").write_text("// brand new\n")
    # dirty outside scope: must NOT be committed
    (git_project / "tests.yaml").write_text("# unrelated WIP\n")
    status_before = _git(git_project, "status", "--porcelain")

    code, payload = _register(git_project, monkeypatch, capsys)
    assert code == 0
    source = payload["record"]["source"]

    # exp branch off the baseline, pin exact
    snapshot = _git(git_project, "rev-parse", "exp/exp-0001")
    assert source["git_sha"] == snapshot
    assert source["branch"] == "exp/exp-0001"
    assert source["dirty"] is False
    assert source["diff_from"] == head
    assert _git(git_project, "rev-parse", "exp/exp-0001^") == head

    # the snapshot holds ONLY the scoped paths
    changed = _git(
        git_project, "diff", "--name-only", f"{head}..{snapshot}"
    ).splitlines()
    assert sorted(changed) == ["src/example.sv", "src/new_block.sv"]
    blob = _git(git_project, "show", f"{snapshot}:src/new_block.sv")
    assert blob == "// brand new"

    # the user's checkout is byte-for-byte undisturbed
    assert _git(git_project, "rev-parse", "HEAD") == head
    assert _git(git_project, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(git_project, "status", "--porcelain") == status_before


def test_auto_clean_tree_converges_to_head_without_branch(
    git_project: Path, monkeypatch, capsys
):
    head = _git(git_project, "rev-parse", "HEAD")
    code, payload = _register(git_project, monkeypatch, capsys)
    assert code == 0
    source = payload["record"]["source"]
    assert source["git_sha"] == head
    assert source["branch"] == "main"
    assert source["dirty"] is False
    assert source["diff_from"] == head
    assert _git(git_project, "branch", "--list", "exp/*") == ""


def test_auto_dirty_outside_scope_records_head(git_project: Path, monkeypatch, capsys):
    _set_cfg(git_project, ['source-scope: ["src"]'])
    head = _git(git_project, "rev-parse", "HEAD")
    (git_project / "tests.yaml").write_text("# WIP outside the design scope\n")
    code, payload = _register(git_project, monkeypatch, capsys)
    assert code == 0
    assert payload["record"]["source"]["git_sha"] == head
    assert _git(git_project, "branch", "--list", "exp/*") == ""


# ---------------------------------------------------------------------------
# commit policy: self-managed
# ---------------------------------------------------------------------------


def test_self_managed_dirty_tree_exits_2(git_project: Path, monkeypatch, capsys):
    _set_cfg(git_project, ['commit-mode: "self-managed"'])
    (git_project / "src" / "example.sv").write_text("// uncommitted\n")
    code, out, err = _run(
        ["--machine", "xplr", "register", "--json", "-"],
        monkeypatch,
        capsys,
        stdin="{}",
    )
    assert code == 2
    envelope = _envelope(out)
    assert envelope["exit_code"] == 2
    assert "commit your changes" in envelope["payload"]["error"]
    assert "self-managed" in err
    # nothing was snapshotted behind the user's back
    assert _git(git_project, "branch", "--list", "exp/*") == ""


def test_self_managed_clean_tree_records_head(git_project: Path, monkeypatch, capsys):
    _set_cfg(git_project, ['commit-mode: "self-managed"'])
    head = _git(git_project, "rev-parse", "HEAD")
    code, payload = _register(git_project, monkeypatch, capsys)
    assert code == 0
    source = payload["record"]["source"]
    assert source["git_sha"] == head
    assert source["dirty"] is False


# ---------------------------------------------------------------------------
# bookkeeping exclusion: the ledger/log never count as source
# ---------------------------------------------------------------------------


@pytest.fixture
def unignored_git_project(minimal_project: Path) -> Path:
    """minimal_project as a git repo with NO .gitignore at all (worst case)."""
    _git(minimal_project, "init", "-q", "-b", "main", ".")
    _git(minimal_project, "add", "-A")
    _git(minimal_project, "commit", "-q", "-m", "init")
    return minimal_project


def test_ledger_and_log_dirt_records_head_and_diffs_as_same_source(
    unignored_git_project: Path, monkeypatch, capsys
):
    project = unignored_git_project
    head = _git(project, "rev-parse", "HEAD")
    code, p1 = _register(project, monkeypatch, capsys)
    assert code == 0
    # exp-0001's record + lock now sit unignored under artefacts/xplr; the
    # rb log file is bookkeeping too — none of it is source
    (project / "rtl_buddy.log").write_text("rb log line\n")
    code, p2 = _register(project, monkeypatch, capsys)
    assert code == 0
    assert p1["record"]["source"]["git_sha"] == head
    assert p2["record"]["source"]["git_sha"] == head
    assert _git(project, "branch", "--list", "exp/*") == ""
    # so the agent's "did the source actually change?" signal works
    code, out, _ = _run(
        ["--machine", "xplr", "diff", "exp-0001", "exp-0002"], monkeypatch, capsys
    )
    assert code == 0
    source = _envelope(out)["payload"]["source"]
    assert "same source revision" in source["note"]


def test_snapshot_excludes_ledger_and_log(
    unignored_git_project: Path, monkeypatch, capsys
):
    project = unignored_git_project
    head = _git(project, "rev-parse", "HEAD")
    code, _ = _register(project, monkeypatch, capsys)  # populates the ledger
    assert code == 0
    (project / "rtl_buddy.log").write_text("rb log line\n")
    (project / "src" / "example.sv").write_text("// tweaked rtl\n")
    code, payload = _register(project, monkeypatch, capsys)
    assert code == 0
    snapshot = payload["record"]["source"]["git_sha"]
    assert snapshot == _git(project, "rev-parse", "exp/exp-0002")
    # only the RTL change is in the snapshot — no record.json, lock, or log
    changed = _git(project, "diff", "--name-only", f"{head}..{snapshot}").splitlines()
    assert changed == ["src/example.sv"]
    tracked = _git(project, "ls-tree", "-r", "--name-only", snapshot).splitlines()
    assert not [
        p for p in tracked if p.startswith("artefacts/") or p == "rtl_buddy.log"
    ]


def test_identical_dirty_rtl_reuses_snapshot_sha(
    git_project: Path, monkeypatch, capsys
):
    (git_project / "src" / "example.sv").write_text("// same dirty state\n")
    code, p1 = _register(git_project, monkeypatch, capsys)
    assert code == 0
    code, p2 = _register(git_project, monkeypatch, capsys)  # RTL unchanged
    assert code == 0
    # identical source pins an identical sha; both exp branches share it
    assert p1["record"]["source"]["git_sha"] == p2["record"]["source"]["git_sha"]
    assert _git(git_project, "rev-parse", "exp/exp-0001") == _git(
        git_project, "rev-parse", "exp/exp-0002"
    )


def test_self_managed_ledger_and_log_dirt_is_not_an_error(
    unignored_git_project: Path, monkeypatch, capsys
):
    project = unignored_git_project
    _set_cfg(project, ['commit-mode: "self-managed"'])
    head = _git(project, "rev-parse", "HEAD")
    code, p1 = _register(project, monkeypatch, capsys)
    assert code == 0
    (project / "rtl_buddy.log").write_text("rb log line\n")
    code, p2 = _register(project, monkeypatch, capsys)  # only bookkeeping dirty
    assert code == 0
    assert p1["record"]["source"]["git_sha"] == head
    assert p2["record"]["source"]["git_sha"] == head


def test_register_warns_when_ledger_not_ignored(
    unignored_git_project: Path, monkeypatch, capsys
):
    code, out, err = _run(
        ["--machine", "xplr", "register", "--json", "-"],
        monkeypatch,
        capsys,
        stdin="{}",
    )
    assert code == 0
    assert "not gitignored" in err


def test_register_does_not_warn_when_ledger_ignored(
    git_project: Path, monkeypatch, capsys
):
    code, out, err = _run(
        ["--machine", "xplr", "register", "--json", "-"],
        monkeypatch,
        capsys,
        stdin="{}",
    )
    assert code == 0
    assert "not gitignored" not in err


# ---------------------------------------------------------------------------
# diff_from baselines
# ---------------------------------------------------------------------------


def test_baseline_option_sets_diff_from(git_project: Path, monkeypatch, capsys):
    base = _git(git_project, "rev-parse", "HEAD")
    (git_project / "src" / "example.sv").write_text("// v2\n")
    _git(git_project, "add", "-A")
    _git(git_project, "commit", "-q", "-m", "v2")
    code, payload = _register(
        git_project, monkeypatch, capsys, extra_argv=["--baseline", "HEAD~1"]
    )
    assert code == 0
    assert payload["record"]["source"]["diff_from"] == base


def test_baseline_unknown_ref_exits_2(git_project: Path, monkeypatch, capsys):
    code, out, _ = _run(
        ["--machine", "xplr", "register", "--baseline", "no-such-ref"],
        monkeypatch,
        capsys,
    )
    assert code == 2
    assert "no-such-ref" in _envelope(out)["payload"]["error"]


def test_parent_pinned_sha_is_default_baseline(git_project: Path, monkeypatch, capsys):
    code, payload = _register(git_project, monkeypatch, capsys)
    assert code == 0
    parent_sha = payload["record"]["source"]["git_sha"]
    (git_project / "src" / "example.sv").write_text("// child variant\n")
    code, payload = _register(
        git_project, monkeypatch, capsys, doc={"knobs": [], "parent": "exp-0001"}
    )
    assert code == 0
    source = payload["record"]["source"]
    assert source["diff_from"] == parent_sha
    assert source["branch"] == "exp/exp-0002"


# ---------------------------------------------------------------------------
# materialize / release
# ---------------------------------------------------------------------------


def test_materialize_release_roundtrip(git_project: Path, monkeypatch, capsys):
    # pin a dirty-tree snapshot so the worktree must hold the variant
    (git_project / "src" / "example.sv").write_text("// snapshot content\n")
    code, payload = _register(git_project, monkeypatch, capsys)
    assert code == 0
    sha = payload["record"]["source"]["git_sha"]

    code, out, _ = _run(
        ["--machine", "xplr", "materialize", "exp-0001"], monkeypatch, capsys
    )
    assert code == 0, out
    info = _envelope(out)["payload"]
    worktree = Path(info["path"])
    assert info["reused"] is False
    assert info["git_sha"] == sha
    assert worktree == git_project / "artefacts" / "xplr" / "worktrees" / "exp-0001"
    assert _git(worktree, "rev-parse", "HEAD") == sha
    assert (worktree / "src" / "example.sv").read_text() == "// snapshot content\n"
    sidecar = git_project / "artefacts" / "xplr" / "exp-0001" / "worktree.json"
    assert json.loads(sidecar.read_text())["path"] == str(worktree)

    # idempotent: a second materialize reuses the same worktree
    code, out, _ = _run(
        ["--machine", "xplr", "materialize", "exp-0001"], monkeypatch, capsys
    )
    assert code == 0
    again = _envelope(out)["payload"]
    assert again["reused"] is True
    assert again["path"] == str(worktree)

    # an explicit conflicting --path fails loudly instead of duplicating
    code, out, _ = _run(
        ["--machine", "xplr", "materialize", "exp-0001", "--path", "elsewhere"],
        monkeypatch,
        capsys,
    )
    assert code == 2
    assert "already materialized" in _envelope(out)["payload"]["error"]

    # release removes the worktree, keeps branch + record
    code, out, _ = _run(
        ["--machine", "xplr", "release", "exp-0001"], monkeypatch, capsys
    )
    assert code == 0
    assert _envelope(out)["payload"]["removed"] is True
    assert not worktree.exists()
    assert not sidecar.exists()
    assert str(worktree) not in _git(git_project, "worktree", "list")
    assert _git(git_project, "rev-parse", "exp/exp-0001") == sha
    assert (git_project / "artefacts" / "xplr" / "exp-0001" / "record.json").is_file()

    # releasing again is a no-op, not an error
    code, out, _ = _run(
        ["--machine", "xplr", "release", "exp-0001"], monkeypatch, capsys
    )
    assert code == 0
    assert _envelope(out)["payload"]["removed"] is False


def test_materialize_unknown_experiment_exits_2(git_project: Path, monkeypatch, capsys):
    code, out, _ = _run(
        ["--machine", "xplr", "materialize", "exp-9999"], monkeypatch, capsys
    )
    assert code == 2
    assert "exp-9999" in _envelope(out)["payload"]["error"]


# ---------------------------------------------------------------------------
# gc
# ---------------------------------------------------------------------------


_DIRECTIONS = {
    "lut_pct": {"direction": "min"},
    "delay_ns": {"direction": "min"},
}


def _experiment_with_outcome(
    project: Path,
    monkeypatch,
    capsys,
    *,
    status: str = "success",
    metrics: dict | None = None,
    parent: str | None = None,
    heavy_kb: int = 4,
) -> str:
    """Register + attach an outcome + drop a heavy artifact file."""
    doc: dict = {"knobs": []}
    if parent is not None:
        doc["parent"] = parent
    code, payload = _register(project, monkeypatch, capsys, doc=doc)
    assert code == 0
    exp_id = payload["id"]
    artifact_rel = f"artefacts/xplr/{exp_id}/build.bin"
    outcome: dict = {"status": status, "artifacts": [artifact_rel]}
    if metrics is not None:
        outcome["metrics"] = metrics
        outcome["metric_meta"] = _DIRECTIONS
    _attach(project, monkeypatch, capsys, exp_id, outcome)
    (project / artifact_rel).write_bytes(b"\0" * (heavy_kb * 1024))
    return exp_id


def _gc_ledger(project: Path, monkeypatch, capsys) -> dict[str, Path]:
    """Four experiments: frontier (exp-0002) + lineage (exp-0001) protected,
    a dominated one (exp-0003) and a failed one (exp-0004) evictable."""
    ids = {}
    ids["lineage"] = _experiment_with_outcome(
        project, monkeypatch, capsys, metrics={"lut_pct": 60, "delay_ns": 6.0}
    )
    ids["frontier"] = _experiment_with_outcome(
        project,
        monkeypatch,
        capsys,
        metrics={"lut_pct": 40, "delay_ns": 4.0},
        parent=ids["lineage"],
    )
    ids["dominated"] = _experiment_with_outcome(
        project, monkeypatch, capsys, metrics={"lut_pct": 70, "delay_ns": 7.0}
    )
    ids["failed"] = _experiment_with_outcome(
        project, monkeypatch, capsys, status="failed"
    )
    return ids


def _gc(monkeypatch, capsys, *argv: str) -> dict:
    code, out, _ = _run(["--machine", "xplr", "gc", *argv], monkeypatch, capsys)
    assert code == 0, out
    return _envelope(out)["payload"]


def test_gc_keep_frontier_protects_frontier_and_lineage(
    git_project: Path, monkeypatch, capsys
):
    ids = _gc_ledger(git_project, monkeypatch, capsys)
    # give the dominated experiment a live worktree too: gc must reap it
    code, out, _ = _run(
        ["--machine", "xplr", "materialize", ids["dominated"]], monkeypatch, capsys
    )
    assert code == 0, out
    worktree = Path(_envelope(out)["payload"]["path"])

    payload = _gc(monkeypatch, capsys, "--target-gb", "0")
    evicted_ids = [entry["id"] for entry in payload["evicted"]]
    assert evicted_ids == [ids["dominated"], ids["failed"]]  # oldest-first
    assert ids["frontier"] in payload["protected"]
    assert ids["lineage"] in payload["protected"]
    assert payload["policy"] == "keep-frontier"
    assert payload["dry_run"] is False
    assert payload["bytes_freed_total"] > 0
    assert payload["usage_bytes_after"] < payload["usage_bytes_before"]

    ledger_root = git_project / "artefacts" / "xplr"
    # heavy artifacts of evicted experiments are gone, records survive
    for key in ("dominated", "failed"):
        assert not (ledger_root / ids[key] / "build.bin").exists()
        assert (ledger_root / ids[key] / "record.json").is_file()
    assert not worktree.exists()
    # protected experiments keep everything
    for key in ("frontier", "lineage"):
        assert (ledger_root / ids[key] / "build.bin").is_file()
        assert (ledger_root / ids[key] / "record.json").is_file()


def test_gc_dry_run_evicts_nothing(git_project: Path, monkeypatch, capsys):
    ids = _gc_ledger(git_project, monkeypatch, capsys)
    payload = _gc(monkeypatch, capsys, "--target-gb", "0", "--dry-run")
    assert payload["dry_run"] is True
    assert [e["id"] for e in payload["evicted"]] == [ids["dominated"], ids["failed"]]
    assert payload["usage_bytes_after"] == payload["usage_bytes_before"]
    ledger_root = git_project / "artefacts" / "xplr"
    for exp_id in ids.values():
        assert (ledger_root / exp_id / "build.bin").is_file()
        assert (ledger_root / exp_id / "record.json").is_file()


def test_gc_manual_policy_lists_candidates_only(git_project: Path, monkeypatch, capsys):
    ids = _gc_ledger(git_project, monkeypatch, capsys)
    payload = _gc(monkeypatch, capsys, "--target-gb", "0", "--policy", "manual")
    assert payload["evicted"] == []
    assert set(payload["candidates"]) >= {ids["dominated"], ids["failed"]}
    assert any("manual" in note for note in payload["notes"])
    ledger_root = git_project / "artefacts" / "xplr"
    for exp_id in ids.values():
        assert (ledger_root / exp_id / "build.bin").is_file()


def test_gc_under_target_is_a_noop(git_project: Path, monkeypatch, capsys):
    _gc_ledger(git_project, monkeypatch, capsys)
    payload = _gc(monkeypatch, capsys)  # default target = 50 GB watermark
    assert payload["evicted"] == []
    assert payload["over_hard_cap"] is False


def test_gc_invalid_policy_exits_2(git_project: Path, monkeypatch, capsys):
    code, out, _ = _run(
        ["--machine", "xplr", "gc", "--policy", "yolo"], monkeypatch, capsys
    )
    assert code == 2
    assert "yolo" in _envelope(out)["payload"]["error"]


def test_register_hard_cap_backstop_blocks_when_gc_cannot_free(
    git_project: Path, monkeypatch, capsys
):
    # ~107 byte watermark + cap: a single protected experiment exceeds it
    _set_cfg(
        git_project,
        ["disk-high-watermark-gb: 0.0000001", "disk-hard-cap-gb: 0.0000001"],
    )
    _experiment_with_outcome(
        git_project,
        monkeypatch,
        capsys,
        metrics={"lut_pct": 40, "delay_ns": 4.0},
        heavy_kb=16,
    )
    code, out, err = _run(
        ["--machine", "xplr", "register", "--json", "-"],
        monkeypatch,
        capsys,
        stdin="{}",
    )
    assert code == 2
    assert "hard cap" in _envelope(out)["payload"]["error"]
    assert "hard cap" in err
    # the frontier member kept its artifacts even though gc was attempted
    assert (git_project / "artefacts" / "xplr" / "exp-0001" / "build.bin").is_file()


def test_cfg_xplr_unknown_key_exits_2(git_project: Path, monkeypatch, capsys):
    _set_cfg(git_project, ['commit-modes: "auto"'])  # typo'd key
    code, out, _ = _run(
        ["--machine", "xplr", "register", "--json", "-"],
        monkeypatch,
        capsys,
        stdin="{}",
    )
    assert code == 2
    assert "commit-modes" in _envelope(out)["payload"]["error"]
