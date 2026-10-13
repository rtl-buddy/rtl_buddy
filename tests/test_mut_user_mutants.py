"""User-supplied patch mutants in ``mut.yaml`` (rtl-buddy/rtl_buddy#785).

The patches are real unified diffs applied by ``git apply``; the FPV oracle is the marker-driven fake from ``test_mut``.
"""

from __future__ import annotations

import difflib
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from rtl_buddy.config.mut import MutSuiteConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.runner.mut_results import ERRORED, KILLED, SURVIVED, MutResults

from test_mut import (
    _DESIGN_SV,
    _STUB_MUTANTS,
    _FakeFpvRunner,
    _ScopeFakeFpvRunner,
    _hier_runner,
    _install_scope_xeno,
    _install_stub_xeno,
    _runner,
    _write_hier_project,
    _write_project,
)


def _diff(new_text: str, *, old_text: str = _DESIGN_SV, path: str = "leaf.sv") -> str:
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


_KILL_SV = _DESIGN_SV.replace("cnt + 1'b1", "cnt + 1'b1; // KILL")
_SURVIVE_SV = _DESIGN_SV.replace("endmodule", "// survive\nendmodule")


def _write_patches(root: Path) -> None:
    mdir = root / "fpv" / "leaf" / "mutants"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "kill.patch").write_text(_diff(_KILL_SV))
    (mdir / "survive.patch").write_text(_diff(_SURVIVE_SV))
    # Written against an older leaf.sv: its context no longer matches.
    stale_old = _DESIGN_SV.replace("cnt + 1'b1", "cnt + 2'd1")
    (mdir / "stale.patch").write_text(
        _diff(stale_old.replace("2'd1", "2'd2"), old_text=stale_old)
    )


_USER_BLOCK = """\
mutants:
  - name: leaf_kill
    patch: mutants/kill.patch
  - name: leaf_survive
    patch: mutants/survive.patch
  - name: leaf_stale
    patch: mutants/stale.patch
"""


def _user_only_project(tmp_path: Path, operators: str = "operators: []") -> Path:
    mut_path = _write_project(tmp_path)
    _write_patches(tmp_path)
    text = mut_path.read_text().replace(
        "operators: [arith_flip, cond_const, assign_drop]\n",
        f"{operators}\n{_USER_BLOCK}" if operators else _USER_BLOCK,
    )
    mut_path.write_text(text)
    return mut_path


# ---- config ------------------------------------------------------------------


def test_config_loads_user_mutants_with_empty_operators(tmp_path):
    cfg = MutSuiteConfig(path=str(_user_only_project(tmp_path))).get_config()
    assert cfg.get_operators() == []
    names = [m.name for m in cfg.get_mutants()]
    assert names == ["leaf_kill", "leaf_survive", "leaf_stale"]
    # Patch paths resolve against the mut.yaml directory.
    assert cfg.get_mutants()[0].patch == str(
        tmp_path / "fpv" / "leaf" / "mutants" / "kill.patch"
    )
    assert cfg.get_mutants()[0].patch_display == "mutants/kill.patch"
    assert cfg.get_mutants()[0].strip == 1


def test_config_operators_key_may_be_omitted_when_mutants_given(tmp_path):
    cfg = MutSuiteConfig(
        path=str(_user_only_project(tmp_path, operators=""))
    ).get_config()
    assert cfg.get_operators() == []
    assert len(cfg.get_mutants()) == 3


def test_config_rejects_no_operators_and_no_mutants(tmp_path):
    mut_path = _write_project(tmp_path)
    mut_path.write_text(
        mut_path.read_text().replace(
            "operators: [arith_flip, cond_const, assign_drop]", "operators: []"
        )
    )
    with pytest.raises(
        FatalRtlBuddyError, match="operators and mutants are both empty"
    ):
        MutSuiteConfig(path=str(mut_path))


@pytest.mark.parametrize(
    "block,match",
    [
        (
            "mutants:\n  - {name: a, patch: mutants/kill.patch}\n"
            "  - {name: a, patch: mutants/survive.patch}\n",
            "duplicate mutant name 'a'",
        ),
        ("mutants:\n  - {name: ../a, patch: mutants/kill.patch}\n", "mutant name"),
        ("mutants:\n  - {name: a, patch: mutants/nope.patch}\n", "patch not found"),
        (
            "mutants:\n  - {name: a, patch: mutants/kill.patch, strip: -1}\n",
            "strip must be >= 0",
        ),
    ],
)
def test_config_rejects_bad_mutant_entries(tmp_path, block, match):
    mut_path = _write_project(tmp_path)
    _write_patches(tmp_path)
    mut_path.write_text(
        mut_path.read_text().replace(
            "operators: [arith_flip, cond_const, assign_drop]\n",
            "operators: []\n" + block,
        )
    )
    with pytest.raises(FatalRtlBuddyError, match=match):
        MutSuiteConfig(path=str(mut_path))


def test_config_rejects_scope_without_operators(tmp_path):
    mut_path = _user_only_project(tmp_path)
    mut_path.write_text(mut_path.read_text() + "scope:\n  include: ['*leaf*']\n")
    with pytest.raises(FatalRtlBuddyError, match="scope selects files"):
        MutSuiteConfig(path=str(mut_path))


# ---- run ---------------------------------------------------------------------


def _run_user_only(tmp_path, monkeypatch) -> MutResults:
    # User-only campaigns must not import xeno: a None entry makes the import raise.
    monkeypatch.setitem(sys.modules, "rtl_buddy_xeno", None)
    runner = _runner(tmp_path, _user_only_project(tmp_path))
    with patch("rtl_buddy.runner.mut_runner.FpvRunner", _FakeFpvRunner):
        return runner.run()


def test_user_only_campaign_scores_each_patch(tmp_path, monkeypatch):
    results = _run_user_only(tmp_path, monkeypatch)

    assert results.baseline_verdict == "fpv=PASS"
    by_id = {o.mutant_id: o for o in results.outcomes}
    assert list(by_id) == ["user_leaf_kill", "user_leaf_survive", "user_leaf_stale"]
    assert by_id["user_leaf_kill"].outcome == KILLED
    assert by_id["user_leaf_kill"].verdict == "fpv=FAIL"
    assert by_id["user_leaf_survive"].outcome == SURVIVED
    stale = by_id["user_leaf_stale"]
    assert stale.outcome == ERRORED
    assert stale.verdict == "ERROR"
    assert stale.diff_summary.startswith("patch mutants/stale.patch does not apply")
    assert results.score() == pytest.approx(0.5)

    for o in results.outcomes:
        assert o.operator == "user"
    assert by_id["user_leaf_kill"].file == "leaf.sv"
    assert by_id["user_leaf_kill"].diff_summary == "patch mutants/kill.patch"


def test_user_mutants_leave_the_source_tree_untouched(tmp_path, monkeypatch):
    original = _DESIGN_SV
    _run_user_only(tmp_path, monkeypatch)
    assert (tmp_path / "design" / "leaf" / "leaf.sv").read_text() == original
    patched = tmp_path / "work" / "user_leaf_kill" / "model_src" / "leaf.sv"
    assert patched.read_text() == _KILL_SV


def test_user_mutants_report_one_entry_each(tmp_path, monkeypatch):
    report = _run_user_only(tmp_path, monkeypatch).as_report()
    assert [m["mutant_id"] for m in report["mutants"]] == [
        "user_leaf_kill",
        "user_leaf_survive",
        "user_leaf_stale",
    ]
    assert (report["killed"], report["survived"], report["errored"]) == (1, 1, 1)
    restored = MutResults.from_report(report)
    assert restored.score() == pytest.approx(0.5)


def test_user_mutants_apply_inside_an_enclosing_git_checkout(tmp_path, monkeypatch):
    """The work dir normally sits inside the project's own checkout; patch paths stay model-relative."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    results = _run_user_only(tmp_path, monkeypatch)
    assert results.killed() == 1
    assert results.survived() == 1


def test_strip_zero_reads_unprefixed_paths(tmp_path, monkeypatch):
    mut_path = _write_project(tmp_path)
    mdir = tmp_path / "fpv" / "leaf" / "mutants"
    mdir.mkdir(parents=True)
    (mdir / "p0.patch").write_text(
        _diff(_KILL_SV).replace("a/leaf.sv", "leaf.sv").replace("b/leaf.sv", "leaf.sv")
    )
    mut_path.write_text(
        mut_path.read_text().replace(
            "operators: [arith_flip, cond_const, assign_drop]\n",
            "operators: []\nmutants:\n  - {name: p0, patch: mutants/p0.patch, strip: 0}\n",
        )
    )
    monkeypatch.setitem(sys.modules, "rtl_buddy_xeno", None)
    runner = _runner(tmp_path, mut_path)
    with patch("rtl_buddy.runner.mut_runner.FpvRunner", _FakeFpvRunner):
        results = runner.run()
    assert [o.outcome for o in results.outcomes] == [KILLED]


def test_a_patch_escaping_the_model_tree_is_errored(tmp_path, monkeypatch):
    mut_path = _write_project(tmp_path)
    mdir = tmp_path / "fpv" / "leaf" / "mutants"
    mdir.mkdir(parents=True)
    (mdir / "escape.patch").write_text(
        "--- a/../escape.sv\n+++ b/../escape.sv\n@@ -0,0 +1 @@\n+// KILL\n"
    )
    mut_path.write_text(
        mut_path.read_text().replace(
            "operators: [arith_flip, cond_const, assign_drop]\n",
            "operators: []\nmutants:\n  - {name: esc, patch: mutants/escape.patch}\n",
        )
    )
    monkeypatch.setitem(sys.modules, "rtl_buddy_xeno", None)
    runner = _runner(tmp_path, mut_path)
    with patch("rtl_buddy.runner.mut_runner.FpvRunner", _FakeFpvRunner):
        results = runner.run()
    assert [o.outcome for o in results.outcomes] == [ERRORED]
    assert not (tmp_path / "work" / "user_esc" / "escape.sv").exists()


def test_user_mutants_run_alongside_generated_ones_outside_the_budget(
    tmp_path, monkeypatch
):
    _install_stub_xeno(monkeypatch, _STUB_MUTANTS)
    mut_path = _write_project(tmp_path)
    _write_patches(tmp_path)
    mut_path.write_text(
        mut_path.read_text()
        .replace("max_mutants: 10", "max_mutants: 1")
        .replace(
            "operators: [arith_flip, cond_const, assign_drop]\n",
            "operators: [arith_flip, cond_const, assign_drop]\n" + _USER_BLOCK,
        )
    )
    runner = _runner(tmp_path, mut_path)
    with patch("rtl_buddy.runner.mut_runner.FpvRunner", _FakeFpvRunner):
        results = runner.run()
    ops = [o.operator for o in results.outcomes]
    # Three user mutants first, then max_mutants=1 generated mutant.
    assert ops == ["user", "user", "user", "arith_flip"]
    assert results.outcomes[-1].mutant_id == "m0000_arith_flip"


def test_scoped_campaign_runs_user_mutants_outside_per_file(tmp_path, monkeypatch):
    mut_path = _write_hier_project(
        tmp_path,
        "mutants:\n  - {name: leaf_kill, patch: kill.patch}\n"
        "scope:\n  include: ['*/leaf.sv']\n",
    )
    (mut_path.parent / "kill.patch").write_text(_diff(_KILL_SV))
    _install_scope_xeno(monkeypatch)
    monkeypatch.setattr(
        "rtl_buddy.runner.mut_runner.MutRunner._scope_graph_json",
        lambda self: {
            "schema_version": "1.1",
            "nodes": [
                {
                    "id": "hier_top.u_alu_a",
                    "source": {"file": str(tmp_path / "design/hier/leaf.sv")},
                }
            ],
        },
    )
    runner = _hier_runner(tmp_path, mut_path)
    with patch("rtl_buddy.runner.mut_runner.FpvRunner", _ScopeFakeFpvRunner):
        results = runner.run()
    assert [(o.mutant_id, o.outcome) for o in results.outcomes] == [
        ("user_leaf_kill", KILLED),
        ("m0000_arith_flip", SURVIVED),
    ]
    assert results.per_file == {"leaf.sv": {KILLED: 0, SURVIVED: 1, ERRORED: 0}}


def test_list_candidates_without_operators_needs_no_xeno(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "rtl_buddy_xeno", None)
    runner = _runner(tmp_path, _user_only_project(tmp_path))
    assert runner.list_candidates() == []


def test_the_not_applied_event_has_a_human_message_case():
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(
        "mut_runner.user_mutant_not_applied",
        {
            "campaign": "leaf",
            "mutant": "user_leaf_stale",
            "patch": "/p/stale.patch",
            "error": "patch does not apply",
        },
    )
    assert "user_leaf_stale" in msg and "/p/stale.patch" in msg
    assert "ERRORED" in msg
