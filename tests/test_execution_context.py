"""Tests that commands anchor artifacts, logs and scratch directories on the primary
config's directory, not the invocation directory.
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
    # Slashes and colons are sanitized so the path stays single-level.
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
    # Explicit CLI output paths anchor to the invocation directory, not the command
    # root.
    assert ctx.resolve_input("report.svg") == invocation.resolve() / "report.svg"
    abs_path = tmp_path / "elsewhere" / "x.txt"
    assert ctx.resolve_input(abs_path) == abs_path.resolve()


def test_attach_file_log_re_anchors_append(tmp_path: Path):
    """Re-attaching the file log to the same path appends instead of truncating.

    The regression orchestrator re-anchors the log per suite and back to the
    regression root, and the final re-attach must keep earlier events.
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

    # First attach truncated; second appended.
    text_a = log_a.read_text()
    assert "first-write" in text_a
    assert "after-suite" in text_a, (
        "re-anchoring to a previously-opened path must append; "
        "found only the second write — earlier events were truncated"
    )
    assert "during-suite" in log_b.read_text()


def test_for_dir_uses_explicit_command_root(tmp_path: Path):
    root = tmp_path / "anchor"
    root.mkdir()
    ctx = ExecutionContext.for_dir(invocation_cwd=tmp_path, command_root=root)
    assert ctx.command_root == root.resolve()
    assert ctx.artifact_root == root.resolve() / "artefacts"
    assert ctx.primary_config is None


def test_for_command_honors_artifact_root_outside_command_tree(tmp_path: Path):
    project = tmp_path / "project"
    cfg = project / "verif" / "block" / "tests.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("rtl-buddy-filetype: test_config\n")
    elsewhere = tmp_path / "scratch_disk" / "rtl_buddy_artefacts"
    elsewhere.mkdir(parents=True)

    ctx = ExecutionContext.for_command(
        invocation_cwd=project,
        primary_config=cfg,
        artifact_root=elsewhere,
    )

    assert ctx.artifact_root == elsewhere.resolve()
    artefact = ctx.artifact_dir("foo")
    assert artefact == elsewhere.resolve() / "foo"
    # The override is independent of command_root.
    assert project.resolve() not in artefact.parents
    # Command root and log path still anchor on the primary config.
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


def test_test_list_from_unrelated_cwd_does_not_pollute_invocation_dir(
    minimal_project: Path, monkeypatch
):
    """`rb test --list` from an unrelated directory leaves that directory empty."""
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

    after = _snapshot_dir(unrelated)
    assert after == set(), (
        f"invocation directory leaked files: {sorted(after)}; "
        "config-driven commands should anchor under the primary config"
    )


def test_test_list_opens_no_log_anywhere(minimal_project: Path, monkeypatch):
    """`rb test --list` opens no log, so it neither fails in a read-only checkout nor
    truncates an earlier run's log.

    `test_filelist_explicit_output_anchors_to_invocation_dir` pins the log
    anchoring for a command that does write one.
    """
    unrelated = minimal_project.parent / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    kept = minimal_project / "rtl_buddy.log"
    kept.write_text("what the run before the listing said\n")

    runner, rb = _runner()
    result = runner.invoke(
        rb.app,
        ["test", "-c", str(minimal_project / "tests.yaml"), "--list"],
    )
    assert result.exit_code == 0, result.output

    assert kept.read_text() == "what the run before the listing said\n"
    assert not (unrelated / "rtl_buddy.log").exists()


def test_filelist_explicit_output_anchors_to_invocation_dir(
    minimal_project: Path, monkeypatch
):
    """``rb filelist <model> <output>`` resolves a relative output path against the
    shell cwd.
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

    assert (unrelated / "out.f").exists()
    # The orchestration log lands under the command root.
    assert (minimal_project / "rtl_buddy.log").exists()


def test_enter_command_context_log_path_override_attaches_there(
    minimal_project: Path,
):
    """``log_path=`` moves the file handler without moving the context, keeping a
    dispatched job's log apart from the head's ``<suite>/rtl_buddy.log``.
    """
    import logging

    from rtl_buddy.logging_utils import setup_logging

    setup_logging(debug=False, verbose=True, color=False, machine=False)

    override = minimal_project / "artefacts" / "basic" / "dispatch" / "job.log"
    rb = RtlBuddy(name="log_override")
    rb.invocation_cwd = minimal_project
    ctx = rb._enter_command_context(
        primary_config=minimal_project / "tests.yaml",
        list_only=True,
        log_path=override,
    )

    handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)
    ]
    assert [h.baseFilename for h in handlers] == [str(override)]
    # The parent dir is created and the context is unchanged.
    assert override.parent.is_dir()
    assert ctx.command_root == minimal_project
    assert ctx.log_path == minimal_project / "rtl_buddy.log"
