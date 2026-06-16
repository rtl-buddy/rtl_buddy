"""Tests for ``rb hub`` model discovery + resolution.

Covers the discovery walk, name-collision error path, and the
explicit ``--models-file`` override behaviour. The hub-side
view-builder is mocked out — these tests pin the discovery contract
without needing the rtl-buddy-view binary installed.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.hub import model_discovery


_MODELS_YAML_A = dedent("""\
    rtl-buddy-filetype: model_config
    models:
      - name: alpha
        filelist: [src/a.sv]
      - name: beta
        filelist: [src/b.sv]
""")

_MODELS_YAML_B = dedent("""\
    rtl-buddy-filetype: model_config
    models:
      - name: beta
        filelist: [other/b.sv]
      - name: gamma
        filelist: [other/c.sv]
""")


def _write_models(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_discover_models_files_finds_yaml_in_tree(tmp_path):
    _write_models(tmp_path / "block_a" / "models.yaml", _MODELS_YAML_A)
    _write_models(tmp_path / "block_b" / "deep" / "models.yaml", _MODELS_YAML_B)
    found = model_discovery.discover_models_files(tmp_path)
    assert sorted(p.relative_to(tmp_path) for p in found) == [
        Path("block_a/models.yaml"),
        Path("block_b/deep/models.yaml"),
    ]


def test_discover_models_files_skips_excluded_dirs(tmp_path):
    """A models.yaml under .git/ / artefacts/ / node_modules/ is
    fixture / vendored data, not a real candidate."""
    _write_models(tmp_path / "block_a" / "models.yaml", _MODELS_YAML_A)
    _write_models(tmp_path / ".git" / "models.yaml", _MODELS_YAML_B)
    _write_models(tmp_path / "artefacts" / "models.yaml", _MODELS_YAML_B)
    _write_models(tmp_path / "node_modules" / "models.yaml", _MODELS_YAML_B)
    found = model_discovery.discover_models_files(tmp_path)
    assert [p.relative_to(tmp_path) for p in found] == [Path("block_a/models.yaml")]


def test_discover_models_files_skips_nested_git_worktrees(tmp_path):
    """A subdirectory whose ``.git`` is a file (the canonical worktree
    marker) is a parallel checkout — its ``models.yaml`` files duplicate
    the parent's and must not enter the candidate set."""
    _write_models(tmp_path / "block_a" / "models.yaml", _MODELS_YAML_A)
    # Simulate a worktree at .worktrees/feature-x/ with the project tree copied in.
    wt = tmp_path / ".worktrees" / "feature-x"
    (wt / ".git").parent.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/feature-x\n")
    _write_models(wt / "block_a" / "models.yaml", _MODELS_YAML_A)
    _write_models(wt / "block_b" / "models.yaml", _MODELS_YAML_B)
    found = model_discovery.discover_models_files(tmp_path)
    assert [p.relative_to(tmp_path) for p in found] == [Path("block_a/models.yaml")]


def test_discover_models_files_keeps_root_git_dir(tmp_path):
    """The starting root's own .git/ (a *directory*, not a file) must not
    trigger the worktree-skip path."""
    (tmp_path / ".git").mkdir()
    _write_models(tmp_path / "block_a" / "models.yaml", _MODELS_YAML_A)
    found = model_discovery.discover_models_files(tmp_path)
    assert [p.relative_to(tmp_path) for p in found] == [Path("block_a/models.yaml")]


def test_discover_models_files_returns_alphabetical_order(tmp_path):
    """Order matters for collision-error messages — must be stable."""
    _write_models(tmp_path / "z_block" / "models.yaml", _MODELS_YAML_A)
    _write_models(tmp_path / "a_block" / "models.yaml", _MODELS_YAML_A)
    _write_models(tmp_path / "m_block" / "models.yaml", _MODELS_YAML_A)
    found = model_discovery.discover_models_files(tmp_path)
    rel = [p.relative_to(tmp_path) for p in found]
    assert rel == sorted(rel)


def test_find_matches_returns_all_files_with_name(tmp_path):
    mf_a = _write_models(tmp_path / "block_a" / "models.yaml", _MODELS_YAML_A)
    mf_b = _write_models(tmp_path / "block_b" / "models.yaml", _MODELS_YAML_B)
    matches = model_discovery.find_matches([mf_a, mf_b], "beta")
    assert sorted(m.models_file for m in matches) == sorted([mf_a, mf_b])


def test_find_matches_zero_when_name_absent(tmp_path):
    mf_a = _write_models(tmp_path / "block_a" / "models.yaml", _MODELS_YAML_A)
    matches = model_discovery.find_matches([mf_a], "no_such_model")
    assert matches == []


def test_find_matches_tolerates_malformed_yaml(tmp_path):
    """A parse error in a sibling models.yaml shouldn't blow up the
    walk — we just don't return it as a match candidate."""
    mf_good = _write_models(tmp_path / "block_a" / "models.yaml", _MODELS_YAML_A)
    mf_bad = _write_models(
        tmp_path / "block_b" / "models.yaml", "not: a valid: schema\n"
    )
    matches = model_discovery.find_matches([mf_good, mf_bad], "alpha")
    assert [m.models_file for m in matches] == [mf_good]


# ---------------------------------------------------------------------------
# resolve_model — the main public entry point
# ---------------------------------------------------------------------------


def test_resolve_model_with_explicit_models_file(tmp_path):
    """``--models-file PATH`` skips discovery and loads the named file."""
    mf = _write_models(tmp_path / "block_a" / "models.yaml", _MODELS_YAML_A)
    _write_models(tmp_path / "block_b" / "models.yaml", _MODELS_YAML_B)  # ignored
    chosen, loader = model_discovery.resolve_model(tmp_path, "beta", models_file=mf)
    assert chosen == mf
    assert loader.get_model("beta").name == "beta"


def test_resolve_model_explicit_models_file_missing_model_raises(tmp_path):
    mf = _write_models(tmp_path / "block_a" / "models.yaml", _MODELS_YAML_A)
    with pytest.raises(FatalRtlBuddyError, match="not found"):
        model_discovery.resolve_model(tmp_path, "no_such", models_file=mf)


def test_resolve_model_explicit_models_file_does_not_exist_raises(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="not a file"):
        model_discovery.resolve_model(
            tmp_path, "alpha", models_file=tmp_path / "missing.yaml"
        )


def test_resolve_model_single_match_via_discovery(tmp_path):
    _write_models(tmp_path / "block_a" / "models.yaml", _MODELS_YAML_A)
    _write_models(tmp_path / "block_b" / "models.yaml", _MODELS_YAML_B)
    chosen, loader = model_discovery.resolve_model(tmp_path, "alpha")
    assert chosen == tmp_path / "block_a" / "models.yaml"
    assert loader.get_model("alpha").name == "alpha"


def test_resolve_model_zero_matches_raises_with_candidates(tmp_path):
    _write_models(tmp_path / "block_a" / "models.yaml", _MODELS_YAML_A)
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        model_discovery.resolve_model(tmp_path, "no_such_model")
    msg = str(excinfo.value)
    assert "no_such_model" in msg
    # candidates listing helps the user spot a typo
    assert "alpha" in msg
    assert "beta" in msg


def test_resolve_model_ambiguous_match_raises_with_paths(tmp_path):
    """``beta`` lives in both files → error names both paths and
    points at --models-file as the fix."""
    mf_a = _write_models(tmp_path / "block_a" / "models.yaml", _MODELS_YAML_A)
    mf_b = _write_models(tmp_path / "block_b" / "models.yaml", _MODELS_YAML_B)
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        model_discovery.resolve_model(tmp_path, "beta")
    msg = str(excinfo.value)
    assert "beta" in msg
    assert str(mf_a) in msg
    assert str(mf_b) in msg
    assert "--models-file" in msg


def test_resolve_model_no_models_yaml_anywhere_raises(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="no models.yaml found"):
        model_discovery.resolve_model(tmp_path, "alpha")
