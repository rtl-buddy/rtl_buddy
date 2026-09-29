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


# --- rb pnr --synth -----------------------------------------------------------


def _synth_driver(monkeypatch, verdicts, synth_verdicts):
    """As `_driver`, with `_do_synth_suite` recording which syntheses ran
    and answering from ``synth_verdicts`` (synth name -> result)."""
    from rtl_buddy.runner.synth_results import SynthFailResults, SynthPassResults

    rb, ran = _driver(monkeypatch, verdicts)
    synths = []
    monkeypatch.setattr(
        "rtl_buddy.rtl_buddy.SynthSuiteConfig", lambda path: ("suite", path)
    )

    def _do_synth_suite(suite_cfg, *, synth_name=None, accept_stale=False, **_kw):
        synths.append((synth_name, accept_stale))
        ran.append(f"synth:{synth_name}")
        if synth_verdicts.get(synth_name, True):
            res = SynthPassResults(name=f"{synth_name}/results")
        else:
            res = SynthFailResults(name=f"{synth_name}/results", desc="yosys failed")
        return [{"synth_name": synth_name, "results": res}]

    rb._do_synth_suite = _do_synth_suite
    return rb, ran, synths


def _synth_run(name, synth, blocks=(), *, reglvl=0):
    return _run(name, blocks, reglvl=reglvl).replace(
        "    synth: s\n", f"    synth: {synth}\n"
    )


def _synth_yaml(path: Path, *names, blocks=None):
    """A real synth.yaml (and the models.yaml it reads) defining ``names``;
    ``blocks`` maps a synthesis to its own `blocks:` list of
    (block, pnr run, pnr-path)."""
    text = "rtl-buddy-filetype: synth_config\nsyntheses:\n"
    for name in names:
        text += (
            f"  - name: {name}\n    desc: {name}\n    model: m\n"
            "    model_path: models.yaml\n    tool: yosys\n"
        )
        entries = (blocks or {}).get(name, ())
        if entries:
            text += "    blocks:\n"
        for block, run, pnr_path in entries:
            text += f"      - {{name: {block}, pnr: {run}, pnr-path: {pnr_path}}}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    (path.parent / "models.yaml").write_text(
        "rtl-buddy-filetype: model_config\nmodels:\n  - name: m\n    filelist: []\n"
    )
    return path


def test_synth_runs_each_synthesis_just_before_its_pnr(tmp_path, monkeypatch):
    """The top's synthesis reads the blocks' abstracts, so it lands after
    their P&R — and a synthesis two P&R runs share runs once."""
    _synth_yaml(tmp_path / "synth.yaml", "top_s", "blk_s")
    suite = _suite(
        tmp_path / "pnr.yaml",
        _synth_run("top", "top_s", [("b", "blk", None)]),
        _synth_run("blk", "blk_s"),
        _synth_run("blk_mc", "blk_s"),
    )
    rb, ran, synths = _synth_driver(monkeypatch, {}, {})

    results = _by_name(rb._do_pnr_suite(suite, run_synth=True, accept_stale=True))

    assert ran == ["synth:blk_s", "blk", "synth:top_s", "top", "blk_mc"]
    assert synths == [("blk_s", True), ("top_s", True)]
    synth = results["top"]["results"].results["synth"]
    assert synth["name"] == "top_s"
    assert synth["result"] == "PASS"
    assert synth["suite"] == str(tmp_path / "synth.yaml")
    row = RtlBuddy._pnr_result_row(rb, results["top"])
    assert row["synth"] == synth
    # Taken up front, once per run that needs it (a re-acquire is a no-op).
    assert set(rb._artifact_locks.acquired) == {tmp_path / "artefacts"}


def test_a_failed_synthesis_fails_its_pnr_and_blocks_the_top(tmp_path, monkeypatch):
    _synth_yaml(tmp_path / "synth.yaml", "top_s", "blk_s")
    suite = _suite(
        tmp_path / "pnr.yaml",
        _synth_run("blk", "blk_s"),
        _synth_run("top", "top_s", [("b", "blk", None)]),
    )
    rb, ran, _synths = _synth_driver(monkeypatch, {}, {"blk_s": False})

    results = _by_name(rb._do_pnr_suite(suite, run_synth=True))

    assert ran == ["synth:blk_s"]
    blk = results["blk"]["results"].results
    assert blk["result"] == "FAIL"
    assert blk["fail_stage"] == "synth"
    assert blk["desc"] == "synthesis 'blk_s' did not pass: yosys failed"
    assert blk["synth"]["result"] == "FAIL"
    assert results["top"]["results"].results["fail_stage"] == "blocked"


def test_without_synth_no_synthesis_runs(tmp_path, monkeypatch):
    suite = _suite(tmp_path / "pnr.yaml", _synth_run("a", "a_s"))
    rb, ran, synths = _synth_driver(monkeypatch, {}, {})

    results = rb._do_pnr_suite(suite)

    assert ran == ["a"]
    assert synths == []
    assert "synth" not in results[0]["results"].results


def test_synth_is_not_run_for_a_pnr_run_that_is_skipped(tmp_path, monkeypatch):
    suite = _suite(tmp_path / "pnr.yaml", _run("a", reglvl=2000))
    rb, ran, synths = _synth_driver(monkeypatch, {}, {})

    rb._do_pnr_suite(suite, reg_level=1000, run_synth=True)

    assert ran == []
    assert synths == []


def test_synth_resolves_every_synthesis_before_anything_runs(tmp_path, monkeypatch):
    """A typo in the top's `synth:` must not wait for its blocks' P&R."""
    _synth_yaml(tmp_path / "synth.yaml", "blk_s")
    suite = _suite(
        tmp_path / "pnr.yaml",
        _synth_run("blk", "blk_s"),
        _synth_run("top", "tpo_s", [("b", "blk", None)]),
    )
    rb, ran, _synths = _synth_driver(monkeypatch, {}, {})

    with pytest.raises(FatalRtlBuddyError, match="tpo_s"):
        rb._do_pnr_suite(suite, run_synth=True)
    assert ran == []


def test_synth_waits_for_the_blocks_its_synthesis_names(tmp_path, monkeypatch):
    """The synthesis reads the abstracts in its own `blocks:`, which the P&R
    run need not list: with --synth those are edges too, and pulled in."""
    _suite(tmp_path / "b" / "pnr.yaml", _synth_run("blk", "blk_s"))
    _synth_yaml(
        tmp_path / "synth.yaml",
        "blk_s",
        "top_s",
        blocks={"top_s": [("b", "blk", "b/pnr.yaml")]},
    )
    _synth_yaml(tmp_path / "b" / "synth.yaml", "blk_s")
    suite = _suite(tmp_path / "pnr.yaml", _synth_run("top", "top_s"))

    plain = plan_pnr_runs(suite)
    with_synth = plan_pnr_runs(suite, synth_blocks=lambda _cfg: True)

    assert _names(plain) == ["top"]
    assert _names(with_synth) == ["blk", "top"]
    assert [d.block for d in with_synth[1].deps] == ["b"]


# --- rb pnr -j ------------------------------------------------------------------


def test_independent_blocks_run_side_by_side_and_the_top_waits(tmp_path):
    """Both blocks are inside `step` at once — neither can finish until the
    other has started — and the top starts only after both finished."""
    import threading

    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("top", [("a", "a", None), ("b", "b", None)]),
        _run("a"),
        _run("b"),
    )
    both_started = threading.Barrier(2, timeout=10)
    events = []
    lock = threading.Lock()

    def step(planned, outcomes):
        with lock:
            events.append(("start", planned.name, sorted(k[1] for k in outcomes)))
        if planned.name in ("a", "b"):
            both_started.wait()
        with lock:
            events.append(("end", planned.name))
        return {"pnr_name": planned.name, "results": PnrPassResults(planned.name)}

    rows = pnr_plan.run_plan(plan_pnr_runs(suite), step, jobs=4)

    assert set(k[1] for k in rows) == {"a", "b", "top"}
    top_start = next(e for e in events if e[:2] == ("start", "top"))
    assert top_start[2] == ["a", "b"]
    assert events.index(top_start) == 4


def test_jobs_caps_how_many_run_at_once(tmp_path):
    import threading
    import time

    suite = _suite(tmp_path / "pnr.yaml", *(_run(f"r{i}") for i in range(6)))
    live = []
    peak = []
    lock = threading.Lock()

    def step(planned, outcomes):
        with lock:
            live.append(planned.name)
            peak.append(len(live))
        time.sleep(0.02)
        with lock:
            live.remove(planned.name)
        return {"pnr_name": planned.name, "results": PnrPassResults(planned.name)}

    pnr_plan.run_plan(plan_pnr_runs(suite), step, jobs=2)

    assert max(peak) == 2


def test_parallel_results_come_back_in_plan_order_with_blocking(tmp_path, monkeypatch):
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("top", [("a", "a", None), ("b", "b", None)]),
        _run("a"),
        _run("b"),
        _run("flat"),
    )
    rb, ran = _driver(monkeypatch, {"b": False})

    results = rb._do_pnr_suite(suite, jobs=3)

    assert [r["pnr_name"] for r in results] == ["a", "b", "top", "flat"]
    assert sorted(ran) == ["a", "b", "flat"]
    top = results[2]["results"].results
    assert top["fail_stage"] == "blocked"
    assert top["blocked_by"] == ["b"]


def test_a_synthesis_two_parallel_runs_share_runs_once(tmp_path, monkeypatch):
    import threading

    _synth_yaml(tmp_path / "synth.yaml", "shared")
    suite = _suite(
        tmp_path / "pnr.yaml",
        _synth_run("x", "shared"),
        _synth_run("y", "shared"),
    )
    rb, _ran, synths = _synth_driver(monkeypatch, {}, {})
    inner = rb._do_synth_suite
    gate = threading.Event()

    def _slow(*args, **kw):
        gate.wait(0.2)
        return inner(*args, **kw)

    rb._do_synth_suite = _slow

    results = rb._do_pnr_suite(suite, run_synth=True, jobs=2)

    assert synths == [("shared", False)]
    assert all(r["results"].results["synth"]["name"] == "shared" for r in results)


def test_once_map_computes_each_key_once_across_threads():
    from concurrent.futures import ThreadPoolExecutor

    calls = []
    once = pnr_plan.OnceMap()

    def compute():
        calls.append(1)
        return "v"

    with ThreadPoolExecutor(8) as pool:
        values = list(pool.map(lambda _: once.get("k", compute), range(32)))

    assert values == ["v"] * 32
    assert calls == [1]


@pytest.mark.parametrize("jobs", [1, 3])
def test_a_run_that_raises_is_its_own_fail_and_keeps_the_others(
    tmp_path, monkeypatch, jobs
):
    """Under -j a sibling still in OpenROAD must not be thrown away with it."""
    suite = _suite(
        tmp_path / "pnr.yaml",
        _run("boom"),
        _run("sibling"),
        _run("top", [("b", "boom", None)]),
    )
    rb, ran = _driver(monkeypatch, {})
    import rtl_buddy.rtl_buddy as rbmod

    real = rbmod.PnrRunner

    class _Raising(real):
        def run(self):
            if self.name == "boom":
                raise RuntimeError("openroad segfaulted")
            return super().run()

    monkeypatch.setattr(rbmod, "PnrRunner", _Raising)

    results = _by_name(rb._do_pnr_suite(suite, jobs=jobs))

    boom = results["boom"]["results"].results
    assert boom["result"] == "FAIL"
    assert boom["fail_stage"] == "error"
    assert "openroad segfaulted" in boom["desc"]
    assert results["sibling"]["results"].is_pass()
    assert results["top"]["results"].results["fail_stage"] == "blocked"
    assert "sibling" in ran
