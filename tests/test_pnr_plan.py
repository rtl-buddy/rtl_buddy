"""Block-before-top ordering of a whole-suite `rb pnr` (#95 step 4)."""

from pathlib import Path

import pytest

from rtl_buddy.config.pnr import PnrSuiteConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner import pnr_plan
from rtl_buddy.runner.pnr_plan import plan_pnr_runs
from rtl_buddy.runner.pnr_results import PnrFailResults, PnrPassResults


def _run(name, blocks=(), *, reglvl=0, xfail=False):
    text = (
        f"  - name: {name}\n"
        f"    desc: {name}\n"
        "    synth: s\n"
        "    synth-path: synth.yaml\n"
        "    platform: p\n"
        f"    reglvl: {reglvl}\n"
    )
    if xfail:
        text += "    xfail: true\n"
    if blocks:
        text += "    blocks:\n"
        for block, run, path in blocks:
            text += f"      - {{name: {block}, pnr: {run}"
            text += f", pnr-path: {path}}}\n" if path else "}\n"
    return text


def _suite(path: Path, *runs: str) -> PnrSuiteConfig:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("rtl-buddy-filetype: pnr_config\nruns:\n" + "".join(runs))
    return PnrSuiteConfig(str(path))


def _names(plan):
    return [p.name for p in plan]


# --- the plan ----------------------------------------------------------------


def test_a_suite_with_no_blocks_keeps_its_file_order(tmp_path):
    suite = _suite(tmp_path / "pnr.yaml", _run("c"), _run("a"), _run("b"))

    plan = plan_pnr_runs(suite)

    assert _names(plan) == ["c", "a", "b"]
    assert all(not p.deps and not p.pulled_in for p in plan)


def test_blocks_go_before_the_run_that_consumes_them(tmp_path):
    """The top is listed first; its two blocks still go first, in file order,
    and the unrelated run keeps its place relative to them."""
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("top", [("csr", "csr_pnr", None), ("cmp", "cmp_pnr", None)]),
        _run("flat"),
        _run("cmp_pnr"),
        _run("csr_pnr"),
    )

    plan = plan_pnr_runs(suite)

    assert _names(plan) == ["flat", "cmp_pnr", "csr_pnr", "top"]
    [top] = [p for p in plan if p.name == "top"]
    assert [d.block for d in top.deps] == ["csr", "cmp"]


def test_a_block_can_itself_be_built_from_blocks(tmp_path):
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("top", [("mid", "mid_pnr", None)]),
        _run("mid_pnr", [("leaf", "leaf_pnr", None)]),
        _run("leaf_pnr"),
    )

    assert _names(plan_pnr_runs(suite)) == ["leaf_pnr", "mid_pnr", "top"]


def test_a_block_in_another_pnr_yaml_is_pulled_in_first(tmp_path):
    """Asking for a suite asks for everything it is built from — and that
    block's own blocks, in their own file, in turn."""
    _suite(tmp_path / "leaf" / "pnr.yaml", _run("leaf_pnr"), _run("unrelated"))
    _suite(
        tmp_path / "blk" / "pnr.yaml",
        _run("blk_pnr", [("leaf", "leaf_pnr", "../leaf/pnr.yaml")]),
    )
    suite = _suite(
        tmp_path / "top" / "pnr.yaml",
        _run("top", [("blk", "blk_pnr", "../blk/pnr.yaml")]),
    )

    plan = plan_pnr_runs(suite)

    assert _names(plan) == ["leaf_pnr", "blk_pnr", "top"]
    assert [p.pulled_in for p in plan] == [True, True, False]
    assert plan[0].suite_path == str((tmp_path / "leaf" / "pnr.yaml").resolve())


def test_a_block_two_runs_share_is_planned_once(tmp_path):
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("a", [("b", "blk", None)]),
        _run("c", [("b", "blk", None)]),
        _run("blk"),
    )

    assert _names(plan_pnr_runs(suite)) == ["blk", "a", "c"]


def test_a_cycle_is_a_config_error_naming_it(tmp_path):
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("free"),
        _run("a", [("b", "b", None)]),
        _run("b", [("c", "c", None)]),
        _run("c", [("a", "a", None)]),
    )

    with pytest.raises(FatalRtlBuddyError, match=r"cycle: a -> b -> c -> a"):
        plan_pnr_runs(suite)


def test_a_run_that_names_itself_as_a_block_is_refused_at_load(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match=r"'a': lists itself under blocks"):
        _suite(tmp_path / "pnr.yaml", _run("a", [("a", "a", None)]))


def test_a_cycle_across_files_names_the_other_file(tmp_path):
    other = tmp_path / "o" / "pnr.yaml"
    _suite(other, _run("x", [("t", "top", "../t/pnr.yaml")]))
    suite = _suite(
        tmp_path / "t" / "pnr.yaml", _run("top", [("x", "x", "../o/pnr.yaml")])
    )

    with pytest.raises(FatalRtlBuddyError, match=r"top -> x \(.*o/pnr.yaml\) -> top"):
        plan_pnr_runs(suite)


def test_a_block_naming_an_undefined_run_is_a_config_error(tmp_path):
    suite = _suite(tmp_path / "pnr.yaml", _run("top", [("b", "nope", None)]))

    with pytest.raises(
        FatalRtlBuddyError, match=r"block 'b' names pnr run 'nope', which .* does not"
    ):
        plan_pnr_runs(suite)


def test_a_block_naming_a_missing_pnr_yaml_is_a_config_error(tmp_path):
    suite = _suite(
        tmp_path / "pnr.yaml", _run("top", [("b", "b_pnr", "gone/pnr.yaml")])
    )

    with pytest.raises(FatalRtlBuddyError, match=r"block 'b' .* does not exist"):
        plan_pnr_runs(suite)


def test_another_file_is_loaded_once(tmp_path):
    _suite(tmp_path / "b" / "pnr.yaml", _run("x"), _run("y"))
    suite = _suite(
        tmp_path / "t" / "pnr.yaml",
        _run("t1", [("x", "x", "../b/pnr.yaml")]),
        _run("t2", [("y", "y", "../b/pnr.yaml")]),
    )
    loads = []

    def _load(path):
        loads.append(path)
        return PnrSuiteConfig(path)

    plan = pnr_plan.plan_pnr_runs(suite, load_suite=_load)

    # Each top as soon as its own block has run.
    assert _names(plan) == ["x", "t1", "y", "t2"]
    assert len(loads) == 1


# --- the driver --------------------------------------------------------------


class _Locks:
    def __init__(self):
        self.acquired = []

    def acquire(self, root, *, command=None):
        self.acquired.append(Path(root))


def _driver(monkeypatch, verdicts):
    """An RtlBuddy whose P&R runner records the order and answers from
    ``verdicts`` (run name -> pass?), default pass."""
    ran = []

    class _Runner:
        def __init__(self, *, name, suite_dir, **_kw):
            self.name = name
            self.suite_dir = suite_dir

        def run(self):
            ran.append(self.name)
            if verdicts.get(self.name, True):
                return PnrPassResults(name=f"{self.name}/results")
            return PnrFailResults(name=f"{self.name}/results", desc="route failed")

    monkeypatch.setattr("rtl_buddy.rtl_buddy.PnrRunner", _Runner)
    rb = RtlBuddy.__new__(RtlBuddy)
    rb.root_cfg = None
    rb._artifact_locks = _Locks()
    return rb, ran


def _by_name(results):
    return {r["pnr_name"]: r for r in results}


def test_the_driver_runs_blocks_first(tmp_path, monkeypatch):
    suite = _suite(
        tmp_path / "pnr.yaml", _run("top", [("b", "blk", None)]), _run("blk")
    )
    rb, ran = _driver(monkeypatch, {})

    results = rb._do_pnr_suite(suite)

    assert ran == ["blk", "top"]
    assert [r["pnr_name"] for r in results] == ["blk", "top"]
    assert all(r["results"].is_pass() for r in results)


def test_a_failed_block_blocks_its_dependents_naming_it(tmp_path, monkeypatch):
    """Not attempted, and FAIL — with a stage no xfail marker excuses — so a
    suite with a broken block never passes. An unrelated run still runs,
    and a run two levels up is blocked through the one in between."""
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("blk"),
        _run("mid", [("b", "blk", None)]),
        _run("top", [("m", "mid", None)], xfail=True),
        _run("flat"),
    )
    rb, ran = _driver(monkeypatch, {"blk": False})

    results = _by_name(rb._do_pnr_suite(suite))

    assert ran == ["blk", "flat"]
    mid = results["mid"]["results"].results
    assert mid["result"] == "FAIL"
    assert mid["fail_stage"] == "blocked"
    assert mid["blocked_by"] == ["b"]
    assert "block 'b' (pnr run 'blk') did not pass" in mid["desc"]
    top = results["top"]["results"]
    assert not top.is_pass()
    assert top.results["blocked_by"] == ["m"]
    row = RtlBuddy._pnr_result_row(rb, results["mid"])
    assert row["fail_stage"] == "blocked"
    assert row["blocked_by"] == ["b"]
    assert results["flat"]["results"].is_pass()


def test_a_block_skipped_by_reglvl_does_not_block_its_top(tmp_path, monkeypatch):
    """The top consumes whatever abstract is published, as a named run
    does; a missing one fails there, naming the block."""
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("blk", reglvl=2000),
        _run("top", [("b", "blk", None)]),
    )
    rb, ran = _driver(monkeypatch, {})

    results = _by_name(rb._do_pnr_suite(suite, reg_level=1000))

    assert ran == ["top"]
    assert results["blk"]["results"].results["result"] == "SKIP"


def test_a_pulled_in_run_runs_in_its_own_suite_under_its_own_lock(
    tmp_path, monkeypatch
):
    other = tmp_path / "blk" / "pnr.yaml"
    _suite(other, _run("blk_pnr"))
    suite = _suite(
        tmp_path / "top" / "pnr.yaml",
        _run("top", [("b", "blk_pnr", "../blk/pnr.yaml")]),
    )
    rb, ran = _driver(monkeypatch, {})

    results = rb._do_pnr_suite(suite)

    assert ran == ["blk_pnr", "top"]
    assert results[0]["suite"] == str(other.resolve())
    assert "suite" not in results[1]
    assert rb._artifact_locks.acquired == [other.resolve().parent / "artefacts"]
    row = RtlBuddy._pnr_result_row(rb, results[0], suite=results[0].get("suite"))
    assert row["suite"] == str(other.resolve())


def test_a_named_run_does_not_pull_in_or_run_its_blocks(tmp_path, monkeypatch):
    """It consumes the published abstract and fails fast without one; it
    never re-runs a block behind the user's back."""
    suite = _suite(
        tmp_path / "pnr.yaml", _run("top", [("b", "blk", None)]), _run("blk")
    )
    rb, ran = _driver(monkeypatch, {})

    rb._do_pnr_suite(suite, pnr_name="top")

    assert ran == ["top"]


def test_a_cycle_fails_the_command_before_anything_runs(tmp_path, monkeypatch):
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("free"),
        _run("a", [("b", "b", None)]),
        _run("b", [("a", "a", None)]),
    )
    rb, ran = _driver(monkeypatch, {})

    with pytest.raises(FatalRtlBuddyError, match="cycle"):
        rb._do_pnr_suite(suite)
    assert ran == []


def test_an_expected_failure_of_a_block_still_blocks_its_top(tmp_path, monkeypatch):
    """An XFAIL passes the suite, and published no abstract all the same."""
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("blk", xfail=True),
        _run("top", [("b", "blk", None)]),
    )
    rb, ran = _driver(monkeypatch, {"blk": False})

    results = _by_name(rb._do_pnr_suite(suite))

    assert ran == ["blk"]
    assert results["blk"]["results"].results["result"] == "XFAIL"
    assert results["blk"]["results"].is_pass()
    assert results["top"]["results"].results["fail_stage"] == "blocked"


def test_a_run_blocked_by_two_blocks_names_both(tmp_path, monkeypatch):
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("x"),
        _run("y"),
        _run("top", [("bx", "x", None), ("by", "y", None)]),
    )
    rb, _ran = _driver(monkeypatch, {"x": False, "y": False})

    top = _by_name(rb._do_pnr_suite(suite))["top"]["results"].results

    assert top["blocked_by"] == ["bx", "by"]
    assert top["desc"].startswith("blocked: blocks 'bx' (pnr run 'x'), 'by'")


def test_a_run_deselected_by_reglvl_is_skip_even_when_its_block_failed(
    tmp_path, monkeypatch
):
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("blk"),
        _run("top", [("b", "blk", None)], reglvl=5),
    )
    rb, _ran = _driver(monkeypatch, {"blk": False})

    top = _by_name(rb._do_pnr_suite(suite))["top"]["results"].results

    assert top["result"] == "SKIP"
    assert "blocked_by" not in top


def test_two_suites_in_one_directory_defining_one_run_name_are_refused(tmp_path):
    _suite(tmp_path / "blocks.yaml", _run("x"))
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("x"),
        _run("top", [("b", "x", "blocks.yaml")]),
    )

    with pytest.raises(FatalRtlBuddyError, match=r"would both write .*artefacts/x"):
        plan_pnr_runs(suite)
