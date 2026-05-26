"""Regression tests for issue #216 — execution-context anchoring.

When ``rb`` is invoked from a directory unrelated to the suite (e.g.
from ``design/<block>/`` with ``-c ../verif/<block>/tests.yaml``), no
artifacts, no orchestration log, no scratch directories should land in
the invocation directory.

The fix introduces :class:`rtl_buddy.exec_context.ExecutionContext` which
anchors each command on ``dirname(primary_config)``. These tests make
sure the anchoring sticks and the invocation directory stays clean.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from rtl_buddy.exec_context import ExecutionContext
from rtl_buddy.rtl_buddy import RtlBuddy


def _runner() -> tuple[CliRunner, RtlBuddy]:
    return CliRunner(), RtlBuddy(name="test_exec_ctx")


def _snapshot_dir(path: Path) -> set[str]:
    return {p.name for p in path.iterdir()}


# ---------------------------------------------------------------------------
# ExecutionContext dataclass behaviour
# ---------------------------------------------------------------------------


def test_for_command_anchors_command_root_on_primary_config(tmp_path: Path):
    cfg = tmp_path / "verif" / "block" / "tests.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("rtl-buddy-filetype: test_config\n")
    invocation = tmp_path / "design" / "block"
    invocation.mkdir(parents=True)

    ctx = ExecutionContext.for_command(
        invocation_cwd=invocation,
        primary_config=Path("../../verif/block/tests.yaml"),
    )

    assert ctx.invocation_cwd == invocation.resolve()
    assert ctx.command_root == cfg.parent.resolve()
    assert ctx.artifact_root == cfg.parent.resolve() / "artefacts"
    assert ctx.log_path == cfg.parent.resolve() / "rtl_buddy.log"


def test_artifact_dir_sanitizes_components(tmp_path: Path):
    ctx = ExecutionContext.for_command(
        invocation_cwd=tmp_path,
        primary_config=tmp_path / "tests.yaml",
    )
    # Slashes / colons get sanitized; the path stays single-level.
    out = ctx.artifact_dir("foo/bar:baz")
    assert out.parent == ctx.artifact_root
    assert "/" not in out.name
    assert ":" not in out.name


def test_resolve_input_anchors_to_invocation_cwd(tmp_path: Path):
    invocation = tmp_path / "design" / "block"
    invocation.mkdir(parents=True)
    cfg_dir = tmp_path / "verif" / "block"
    cfg_dir.mkdir(parents=True)
    cfg = cfg_dir / "tests.yaml"
    cfg.write_text("")

    ctx = ExecutionContext.for_command(
        invocation_cwd=invocation,
        primary_config=cfg,
    )
    # Explicit CLI output paths follow shell semantics — they anchor to
    # the directory the user invoked from, not the command root.
    assert ctx.resolve_input("report.svg") == invocation.resolve() / "report.svg"
    # Absolute paths pass through.
    abs_path = tmp_path / "elsewhere" / "x.txt"
    assert ctx.resolve_input(abs_path) == abs_path.resolve()


def test_attach_file_log_re_anchors_append(tmp_path: Path):
    """Re-attaching the file log to the same path appends, not truncates.

    This is the contract the regression orchestrator relies on: it
    attaches the log to ``dirname(regression.yaml)/rtl_buddy.log``,
    re-anchors per suite, and finally re-anchors back to the regression
    root for the summary phase. The final re-attach must not erase the
    pre-loop events.
    """
    import logging

    from rtl_buddy.logging_utils import attach_file_log, setup_logging

    log_a = tmp_path / "a" / "rtl_buddy.log"
    log_b = tmp_path / "b" / "rtl_buddy.log"
    log_a.parent.mkdir()
    log_b.parent.mkdir()

    setup_logging(debug=False, verbose=True, color=False, machine=False)

    test_logger = logging.getLogger("rtl_buddy.test_attach")

    attach_file_log(log_a)
    test_logger.info("first-write")

    attach_file_log(log_b)
    test_logger.info("during-suite")

    attach_file_log(log_a)
    test_logger.info("after-suite")

    # First attach to log_a truncated; second appended.
    text_a = log_a.read_text()
    assert "first-write" in text_a
    assert "after-suite" in text_a, (
        "re-anchoring to a previously-opened path must append; "
        "found only the second write — earlier events were truncated"
    )
    # log_b is independent.
    assert "during-suite" in log_b.read_text()


def test_for_dir_uses_explicit_command_root(tmp_path: Path):
    root = tmp_path / "anchor"
    root.mkdir()
    ctx = ExecutionContext.for_dir(invocation_cwd=tmp_path, command_root=root)
    assert ctx.command_root == root.resolve()
    assert ctx.artifact_root == root.resolve() / "artefacts"
    assert ctx.primary_config is None


# ---------------------------------------------------------------------------
# Artifact root redirection — forward-ready for `--artifact-root` (see PR #219
# review). Exercises the dataclass-level override that lets a future CLI flag
# send artefacts onto a different disk, outside the root_config.yaml tree.
# ---------------------------------------------------------------------------


def test_for_command_honors_artifact_root_outside_command_tree(tmp_path: Path):
    project = tmp_path / "project"
    cfg = project / "verif" / "block" / "tests.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("rtl-buddy-filetype: test_config\n")
    # Artefact target lives in a completely separate subtree — outside the
    # project root that holds root_config.yaml.
    elsewhere = tmp_path / "scratch_disk" / "rtl_buddy_artefacts"
    elsewhere.mkdir(parents=True)

    ctx = ExecutionContext.for_command(
        invocation_cwd=project,
        primary_config=cfg,
        artifact_root=elsewhere,
    )

    # Override is honored; downstream artefact paths land outside the project tree.
    assert ctx.artifact_root == elsewhere.resolve()
    artefact = ctx.artifact_dir("foo")
    assert artefact == elsewhere.resolve() / "foo"
    # The override is independent of command_root — the project tree
    # containing root_config.yaml is not in the artefact path.
    assert project.resolve() not in artefact.parents
    # Command root + log path still anchor on the primary config (only the
    # artefact tree is redirected).
    assert ctx.command_root == cfg.parent.resolve()
    assert ctx.log_path == cfg.parent.resolve() / "rtl_buddy.log"


def test_for_dir_honors_artifact_root_outside_command_tree(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "scratch_disk" / "artefacts"

    ctx = ExecutionContext.for_dir(
        invocation_cwd=tmp_path,
        command_root=project,
        artifact_root=elsewhere,
    )

    assert ctx.artifact_root == elsewhere.resolve()
    assert ctx.command_root == project.resolve()
    assert project.resolve() not in ctx.artifact_dir("x").parents


# ---------------------------------------------------------------------------
# End-to-end: invoking from an unrelated directory keeps it clean (#216)
# ---------------------------------------------------------------------------


def test_test_list_from_unrelated_cwd_does_not_pollute_invocation_dir(
    minimal_project: Path, monkeypatch
):
    """Repro of #216: run ``rb test --list`` from a sibling directory that
    has no relationship to the suite, and confirm it stays clean.
    """
    unrelated = minimal_project.parent / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    before = _snapshot_dir(unrelated)
    assert before == set()

    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["test", "-c", str(minimal_project / "tests.yaml"), "--list"],
    )
    assert result.exit_code == 0, result.output

    # The unrelated directory must remain empty — no rtl_buddy.log, no
    # verif/, no artefacts/.
    after = _snapshot_dir(unrelated)
    assert after == set(), (
        f"invocation directory leaked files: {sorted(after)}; "
        "config-driven commands should anchor under the primary config"
    )


def test_test_list_writes_log_under_command_root(minimal_project: Path, monkeypatch):
    """The orchestration log lands next to tests.yaml, not in the cwd."""
    unrelated = minimal_project.parent / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["test", "-c", str(minimal_project / "tests.yaml"), "--list"],
    )
    assert result.exit_code == 0, result.output

    # Log is under dirname(tests.yaml).
    assert (minimal_project / "rtl_buddy.log").exists()
    assert not (unrelated / "rtl_buddy.log").exists()


def test_filelist_explicit_output_anchors_to_invocation_dir(
    minimal_project: Path, monkeypatch
):
    """``rb filelist <model> <output>`` follows shell semantics for the
    output path — relative names land in the user's shell cwd.
    """
    unrelated = minimal_project.parent / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        [
            "filelist",
            "example",
            "out.f",
            "-c",
            str(minimal_project / "models.yaml"),
        ],
    )
    assert result.exit_code == 0, result.output

    # The explicit output path is relative to the invocation directory.
    assert (unrelated / "out.f").exists()
    # And the orchestration log lands under the command root (models.yaml dir).
    assert (minimal_project / "rtl_buddy.log").exists()
