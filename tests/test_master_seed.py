"""Master-seed runs (--seed) and per-test `seed:` directives (#566).

A master seed turns into each test's sim seed by pure derivation over the
suite's identity, the expanded test name, and the run id — never dispatch
order or process timing — so re-passing the same ``--seed`` replays a run
exactly without reading any old artefact. These cover the derivation
itself, the `seed:` pin/opt-out, the `sim-rand-seed-plusarg` injection a
preproc hook reads, the CLI surface (exclusivity, dispatch argv + plan
manifest), and the records the seed lands in (test.randseed, result
envelope, structured logs).
"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import pytest
from typer.testing import CliRunner

import rtl_buddy.tools.vlog_sim as vlog_sim_module
import rtl_buddy.rtl_buddy as rtl_buddy_module
from rtl_buddy.dispatch.argv import test_job_argv as _test_job_argv
from rtl_buddy.dispatch.base import DispatchBackend
from rtl_buddy.dispatch.plan import read_plan_seed, write_plan
from rtl_buddy.process_utils import ManagedProcessResult
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.test_results import TestPassResults
from rtl_buddy.seed_mode import SeedMode, derive_seed, seed_identity_for


# ---------------------------------------------------------------------------
# derivation


def test_derive_seed_is_deterministic_and_order_independent():
    """Same inputs → same seed regardless of which order tests ran in."""
    a = derive_seed(7, "tests.yaml", "basic", None)
    b = derive_seed(7, "tests.yaml", "extra", None)
    # Recomputing after the sibling gives the same value: no shared counter.
    assert derive_seed(7, "tests.yaml", "basic", None) == a
    assert a != b
    # run_id and master both mix in.
    assert derive_seed(7, "tests.yaml", "basic", 1) != a
    assert derive_seed(8, "tests.yaml", "basic", None) != a


def test_derive_seed_stays_in_simulator_range():
    for master in (0, 1, 2**31 - 1):
        seed = derive_seed(master, "suite", "name", 12)
        assert 0 <= seed < 2**31


def test_seed_identity_is_relative_under_base():
    assert seed_identity_for("/proj/tests.yaml", "/proj") == "tests.yaml"
    assert seed_identity_for("/proj/sub/s.yaml", "/proj") == "sub/s.yaml"


def test_seed_identity_falls_back_to_absolute_outside_base():
    assert seed_identity_for("/elsewhere/s.yaml", "/proj") == "/elsewhere/s.yaml"


# ---------------------------------------------------------------------------
# sim-level resolution: the dummies vlog_sim tests use, plus a `seed:`


class _DummyBuilderCfg:
    def __init__(self, seed=1234):
        self.seed = seed

    def get_exe(self):
        return "vcs"

    def get_name(self):
        return "stub"

    def get_simv(self):
        return "simv"

    def get_simulator_family(self):
        return "vcs"

    def get_compile_time_opts(self, _mode):
        return []

    def get_seed(self):
        return self.seed

    def get_run_time_opts(self, _mode, seed=None):
        return [f"+seed={seed}"]


class _DummyRootCfg:
    def __init__(self, builder_cfg):
        self.builder_cfg = builder_cfg

    def get_rtl_builder_cfg(self):
        return self.builder_cfg

    def resolve_rtl_builder_cfg(self, _name=None):
        return self.builder_cfg

    def resolve_extra_sim_timeout(self, _cfg):
        return None

    def get_use_lcov(self, _family):
        return False


class _DummyTb:
    def get_filelist(self):
        return []

    def is_cocotb(self):
        return False

    def is_systemc(self):
        return False


class _DummyModel:
    def get_filelist(self):
        return []

    def get_model_path(self):
        return ""


class _SeedTestCfg:
    """TestConfig-shaped double with a configurable `seed:` directive."""

    def __init__(self, name="basic", seed=None, seed_plusarg=None, preproc=None):
        self.name = name
        self.model = _DummyModel()
        self.tb = _DummyTb()
        self.pd = None
        self.uvm = None
        self.pa = {}
        self._seed = seed
        self._seed_plusarg = seed_plusarg
        self._preproc = preproc
        self.resolved_seed = None

    def get_name(self):
        return self.name

    def get_builder_name(self):
        return None

    def get_model(self):
        return self.model

    def get_testbench(self):
        return self.tb

    def get_plusargs(self):
        return self.pa

    def get_plusarg(self, key):
        return self.pa.get(key)

    def set_plusarg(self, key, value):
        self.pa[key] = value

    def get_plusdefines(self):
        return {}

    def get_timeout(self):
        return 60, False

    def get_preproc_path(self):
        return self._preproc

    def get_seed(self):
        return self._seed

    def get_sim_rand_seed_plusarg(self):
        return self._seed_plusarg

    def get_resolved_seed(self):
        return self.resolved_seed

    def set_resolved_seed(self, seed):
        self.resolved_seed = seed


def _make_sim(tmp_path, monkeypatch, *, test_cfg=None, **kwargs):
    monkeypatch.chdir(tmp_path)
    builder_cfg = kwargs.pop("builder_cfg", None) or _DummyBuilderCfg()
    test_cfg = test_cfg or _SeedTestCfg()
    return vlog_sim_module.VlogSim(
        name="rtl_buddy/vlog_sim",
        root_cfg=_DummyRootCfg(builder_cfg),
        test_cfg=test_cfg,
        rtl_builder_mode="sim",
        sim_mode={"sim_to_stdout": False},
        suite_dir=str(tmp_path),
        **kwargs,
    )


def _stub_process_run(monkeypatch, captured):
    """Run every managed process successfully, recording its command."""
    monkeypatch.setattr(vlog_sim_module, "task_status", lambda *a, **k: nullcontext())

    def _fake_run(cmd, stdout=None, **kwargs):
        captured.append(list(cmd))
        if stdout is not None:
            stdout.write("PASS basic\n")
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _fake_run)


def _seed_arg(captured):
    """The ``+seed=<n>`` the sim ran with, from the recorded command."""
    for cmd in captured:
        for arg in cmd:
            if arg.startswith("+seed="):
                return int(arg.removeprefix("+seed="))
    return None


def test_master_seed_derives_and_records_the_run_seed(tmp_path, monkeypatch):
    captured = []
    _stub_process_run(monkeypatch, captured)
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        master_seed=7,
        seed_identity="tests.yaml",
        seed_mode=SeedMode.DEFAULT,
    )

    assert sim.execute() == 0

    expected = derive_seed(7, "tests.yaml", "basic", None)
    assert _seed_arg(captured) == expected
    randseed = Path(sim._get_randseed_path(run_id=None)).read_text()
    assert randseed == f"{expected}\n"
    assert sim.last_seed == expected
    # Results carry it for the envelope/summary (#566).
    results = sim.post()
    assert results.results["seed"] == expected


def test_master_seed_replays_exactly_without_old_artefacts(tmp_path, monkeypatch):
    """Re-running --seed 7 derives the same seed — no test.randseed read."""
    seeds = []
    for _ in range(2):
        captured = []
        _stub_process_run(monkeypatch, captured)
        sim = _make_sim(
            tmp_path,
            monkeypatch,
            master_seed=7,
            seed_identity="tests.yaml",
            seed_mode=SeedMode.DEFAULT,
        )
        assert sim.execute() == 0
        seeds.append(_seed_arg(captured))
    assert seeds[0] == seeds[1]


def test_seed_pin_wins_over_master_and_modes(tmp_path, monkeypatch):
    captured = []
    _stub_process_run(monkeypatch, captured)
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        test_cfg=_SeedTestCfg(seed=4242),
        master_seed=7,
        seed_identity="tests.yaml",
        seed_mode=SeedMode.NEW,
    )
    assert sim.execute() == 0
    assert _seed_arg(captured) == 4242


def test_seed_default_opts_out_of_master_and_new(tmp_path, monkeypatch):
    captured = []
    _stub_process_run(monkeypatch, captured)
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        test_cfg=_SeedTestCfg(seed="default"),
        builder_cfg=_DummyBuilderCfg(seed=31310),
        master_seed=7,
        seed_identity="tests.yaml",
        seed_mode=SeedMode.NEW,
    )
    assert sim.execute() == 0
    assert _seed_arg(captured) == 31310


def test_seed_plusarg_reaches_hook_and_simv(tmp_path, monkeypatch):
    """`sim-rand-seed-plusarg:` injects the resolved seed before PRE."""
    seen = tmp_path / "hook_seen.txt"
    preproc = tmp_path / "preproc.py"
    preproc.write_text(
        "from pathlib import Path\n"
        f"Path({str(seen)!r}).write_text("
        "str(test_cfg.get_resolved_seed()) + '/' + str(test_cfg.get_plusarg('RBSEED')))\n"
    )
    captured = []
    _stub_process_run(monkeypatch, captured)
    test_cfg = _SeedTestCfg(seed_plusarg="RBSEED", preproc=str(preproc))
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        test_cfg=test_cfg,
        master_seed=7,
        seed_identity="tests.yaml",
        seed_mode=SeedMode.DEFAULT,
    )

    assert sim.pre() is None
    expected = derive_seed(7, "tests.yaml", "basic", None)
    # The hook saw the value the sim is about to run with.
    assert seen.read_text() == f"{expected}/{expected}"
    assert sim.execute() == 0
    assert f"+RBSEED={expected}" in captured[-1]


def test_no_master_seed_keeps_builder_config_seed(tmp_path, monkeypatch):
    captured = []
    _stub_process_run(monkeypatch, captured)
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        builder_cfg=_DummyBuilderCfg(seed=31310),
        seed_mode=SeedMode.DEFAULT,
    )
    assert sim.execute() == 0
    assert _seed_arg(captured) == 31310


def test_hook_mutating_seed_directive_cannot_diverge_sim(tmp_path, monkeypatch):
    """A preproc that rewrites `seed:` mid-flight must not split stimulus
    and simv onto different seeds: PRE's resolution is authoritative."""
    preproc = tmp_path / "preproc.py"
    preproc.write_text("test_cfg.seed = 9999\n")
    captured = []
    _stub_process_run(monkeypatch, captured)
    test_cfg = _SeedTestCfg(preproc=str(preproc))
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        test_cfg=test_cfg,
        master_seed=7,
        seed_identity="tests.yaml",
        seed_mode=SeedMode.DEFAULT,
    )

    assert sim.pre() is None
    assert sim.execute() == 0
    expected = derive_seed(7, "tests.yaml", "basic", None)
    assert _seed_arg(captured) == expected


def test_timed_out_run_records_its_seed(tmp_path, monkeypatch):
    """A 4444 timeout never reaches post(); the runner must still stamp
    the launched seed into the result envelope (#566)."""
    from rtl_buddy.runner.test_runner import TestRunner

    monkeypatch.setattr(vlog_sim_module, "task_status", lambda *a, **k: nullcontext())
    monkeypatch.setattr(
        vlog_sim_module,
        "run_managed_process",
        lambda *a, **k: ManagedProcessResult(returncode=4444, timed_out=True),
    )
    root_cfg = _DummyRootCfg(_DummyBuilderCfg())
    root_cfg.get_project_rootdir = lambda: str(tmp_path)
    runner = TestRunner(
        name="t/testrunner",
        root_cfg=root_cfg,
        test_cfg=_SeedTestCfg(),
        test_runner_mode={"sim_to_stdout": False},
        rtl_builder_mode="sim",
        run_depth=None,
        suite_dir=str(tmp_path),
        master_seed=7,
        seed_identity="tests.yaml",
    )
    # compile() would invoke the builder; skip straight past it.
    monkeypatch.setattr(TestRunner, "compile_prepared", lambda self, run_ids=None: None)

    res = runner.run()

    expected = derive_seed(7, "tests.yaml", "basic", None)
    assert res.results["result"] == "FAIL"
    assert res.results["seed"] == expected


# ---------------------------------------------------------------------------
# CLI surface


class _StubRunner:
    """TestRunner stand-in: records ctor kwargs, returns a canned result."""

    canned = None
    inits = []

    def __init__(self, **kwargs):
        type(self).inits.append(kwargs)

    def run(self):
        return type(self).canned

    def run_multiple(self, run_ids):
        return [type(self).canned for _ in run_ids]

    @property
    def last_compile(self):
        return None


@pytest.fixture
def stub_runner(monkeypatch: pytest.MonkeyPatch) -> type[_StubRunner]:
    _StubRunner.canned = TestPassResults(name="basic/results")
    _StubRunner.inits = []
    monkeypatch.setattr(rtl_buddy_module, "TestRunner", _StubRunner)
    return _StubRunner


def _invoke(args):
    runner, rb = CliRunner(), RtlBuddy(name="test_master_seed")
    return runner.invoke(rb.app, args), rb


def test_seed_and_rnd_new_are_mutually_exclusive(minimal_project, stub_runner):
    result, _ = _invoke(["test", "basic", "--seed", "7", "--rnd-new"])
    assert result.exit_code != 0
    assert "mutually exclusive" in str(result.exception)


def test_seed_and_rnd_last_are_mutually_exclusive(minimal_project, stub_runner):
    result, _ = _invoke(["test", "basic", "--seed", "7", "--rnd-last"])
    assert result.exit_code != 0
    assert "mutually exclusive" in str(result.exception)


def test_negative_seed_is_rejected(minimal_project, stub_runner):
    result, _ = _invoke(["test", "basic", "--seed", "-1"])
    assert result.exit_code != 0
    assert "non-negative" in str(result.exception)


def test_rb_test_seed_reaches_runner(minimal_project, stub_runner):
    result, _ = _invoke(["test", "basic", "--seed", "7"])
    assert result.exit_code == 0, result.output
    init = _StubRunner.inits[0]
    assert init["master_seed"] == 7
    # Identity is the suite config's path relative to the invocation dir.
    assert init["seed_identity"] == "tests.yaml"


def test_rb_test_without_seed_passes_none(minimal_project, stub_runner):
    result, _ = _invoke(["test", "basic"])
    assert result.exit_code == 0, result.output
    assert _StubRunner.inits[0]["master_seed"] is None


def test_rb_regression_seed_reaches_runner(minimal_project, stub_runner):
    result, _ = _invoke(["regression", "-c", "regression.yaml", "--seed", "9"])
    assert result.exit_code == 0, result.output
    assert _StubRunner.inits[0]["master_seed"] == 9
    assert _StubRunner.inits[0]["seed_identity"] == "tests.yaml"


def test_test_job_seed_flag_reaches_runner(minimal_project, stub_runner):
    result, _ = _invoke(
        ["_test-job", "basic", "--result-json", "res.json", "--seed", "9"]
    )
    assert result.exit_code == 0, result.output
    assert _StubRunner.inits[0]["master_seed"] == 9


def test_test_job_plan_seed_wins_over_argv(minimal_project, stub_runner):
    """The head's recorded master seed is authoritative in a dispatch plan."""
    from rtl_buddy.config import SuiteConfig

    suite_cfg = SuiteConfig(path="tests.yaml")
    plan = write_plan(
        minimal_project / "plan.json",
        "tests.yaml",
        suite_cfg.get_tests(),
        "tok",
        master_seed=11,
        seed_identity="tests.yaml",
    )
    result, _ = _invoke(
        [
            "_test-job",
            "basic",
            "--result-json",
            "res.json",
            "--plan",
            str(plan),
            "--seed",
            "9",
        ]
    )
    assert result.exit_code == 0, result.output
    init = _StubRunner.inits[0]
    assert init["master_seed"] == 11
    assert init["seed_identity"] == "tests.yaml"


# ---------------------------------------------------------------------------
# dispatch: plan manifest + argv carry the master seed


class _FakeBackend(DispatchBackend):
    """Backend double: records the specs rb regression fans out with."""

    name = "fake"

    def __init__(self):
        self.submitted = []
        self.build_submitted = []

    def submit_build(self, spec):
        from rtl_buddy.dispatch.base import JobHandle

        self.build_submitted.append(spec)
        return JobHandle(job_id="fake-build", spec=spec)

    def submit(self, spec, *, dependency=None, delay_sec=0.0):
        from rtl_buddy.dispatch.base import JobHandle
        from rtl_buddy.runner.result_io import write_result_json
        from rtl_buddy.dispatch.plan import read_plan_token

        self.submitted.append(spec)
        write_result_json(
            spec.result_json,
            test_name=spec.test_name,
            run_id=spec.run_id,
            results=TestPassResults(name=spec.test_name + "/results"),
            run_token=read_plan_token(spec.plan_path) if spec.plan_path else None,
        )
        return JobHandle(job_id=f"fake-{len(self.submitted)}", spec=spec)

    def wait_all(self, handles, *, extra_wait=0.0):
        pass

    def cancel_all(self, handles):
        pass

    def advance(self):
        pass


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> _FakeBackend:
    backend = _FakeBackend()
    monkeypatch.setattr(
        rtl_buddy_module, "create_dispatch_backend", lambda *a, **k: backend
    )
    return backend


def _mark_stub_builder_verilator(project: Path):
    root_cfg = project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text().replace(
            '    builder: "echo"\n',
            '    builder: "echo"\n    simulator-family: "verilator"\n',
        )
    )


def test_dispatched_regression_records_seed_in_plan_and_specs(
    minimal_project, fake_backend, stub_runner
):
    _mark_stub_builder_verilator(minimal_project)
    result, _ = _invoke(
        ["regression", "-c", "regression.yaml", "--seed", "7", "--dispatch", "slurm"]
    )
    assert result.exit_code == 0, result.output

    # One spec per runnable test; every one carries the master seed.
    assert fake_backend.submitted
    assert all(spec.master_seed == 7 for spec in fake_backend.submitted)

    # The plan manifest records master + identity; jobs read it back.
    plan_path = fake_backend.submitted[0].plan_path
    master, identity = read_plan_seed(plan_path)
    assert master == 7
    assert identity == "tests.yaml"

    # And the argv a backend builds carries --seed for the fallback path.
    argv = _test_job_argv(fake_backend.submitted[0])
    i = argv.index("--seed")
    assert argv[i + 1] == "7"


def test_dispatched_tests_derive_distinct_seeds(minimal_project, fake_backend):
    """Each expanded test in a seeded run gets its own deterministic seed."""
    result, _ = _invoke(["test", "--seed", "7", "--dispatch", "slurm"])
    assert result.exit_code == 0, result.output
    names = [spec.test_name for spec in fake_backend.submitted]
    seeds = [derive_seed(7, "tests.yaml", name, None) for name in names]
    assert len(set(seeds)) == len(names) == 2


# ---------------------------------------------------------------------------
# config: `seed:` parsing/validation


def test_seed_directive_int_and_default_load(minimal_project):
    from rtl_buddy.config import SuiteConfig

    (minimal_project / "tests.yaml").write_text(
        (minimal_project / "tests.yaml")
        .read_text()
        .replace("    sim_timeout:\n", "    sim_timeout:\n    seed: 5\n", 1)
    )
    cfg = list(SuiteConfig(path="tests.yaml").get_tests())[0]
    assert cfg.get_seed() == 5


def test_seed_directive_rejects_garbage(minimal_project):
    from rtl_buddy.config import SuiteConfig
    from rtl_buddy.errors import FatalRtlBuddyError

    (minimal_project / "tests.yaml").write_text(
        (minimal_project / "tests.yaml")
        .read_text()
        .replace("    sim_timeout:\n", "    sim_timeout:\n    seed: banana\n", 1)
    )
    with pytest.raises(FatalRtlBuddyError, match="seed"):
        SuiteConfig(path="tests.yaml").get_tests()
