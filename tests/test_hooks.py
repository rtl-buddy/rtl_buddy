# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Hook exec() namespace: __name__ sentinel contract (issue #328).

Hook scripts (`preproc`, `sweep`) are exec()'d into a hand-built namespace.
Without an explicit `__name__` key, `if __name__ == "__main__":` guards in
a hook body silently no-op. `build_hook_namespace()` fixes this by always
setting `__name__` to the `HOOK_MODULE_NAME` sentinel, never `"__main__"`.
These tests exercise both exec() sites (VlogSim.pre() for preproc,
RtlBuddy._expand_tests_with_sweep() for sweep) plus the helper directly.

The last section covers issue #371: a hook's own `print()` used to land on
`rtl_buddy`'s stdout, which under `--machine` is the envelope stream.
"""

import json
from pathlib import Path

from rtl_buddy.hooks import HOOK_MODULE_NAME, build_hook_namespace, exec_hook_script
from rtl_buddy.logging_utils import setup_logging
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.test_runner import RunDepth
from rtl_buddy.runner.test_runner import TestRunner as RtlBuddyTestRunner
from rtl_buddy.tools.vlog_sim import VlogSim


def test_build_hook_namespace_sets_sentinel_and_file(tmp_path):
    script = tmp_path / "sub" / "hook.py"
    script.parent.mkdir()
    script.write_text("pass\n")

    ns = build_hook_namespace(str(script), foo="bar")

    assert ns["foo"] == "bar"
    assert ns["__name__"] == HOOK_MODULE_NAME == "__rtl_buddy_hook__"
    assert ns["__file__"] == str(script.resolve())


# --- preproc (VlogSim.pre()) -------------------------------------------------


class DummyBuilderCfg:
    def get_exe(self):
        return "vcs"

    def get_name(self):
        return "vcs"

    def get_compile_time_opts(self, _mode):
        return []

    def get_simulator_family(self):
        return "vcs"

    def get_simv(self):
        return "simv"

    def get_seed(self):
        return 31310

    def get_run_time_opts(self, _mode, seed):
        return []


class DummyRootCfg:
    def get_rtl_builder_cfg(self):
        return DummyBuilderCfg()

    def resolve_rtl_builder_cfg(self, _test_builder_name=None):
        return DummyBuilderCfg()

    def resolve_extra_sim_timeout(self, _builder_cfg):
        return None

    def get_use_lcov(self, _simulator_name):
        return False


class DummyTestbench:
    def get_filelist(self):
        return []

    def is_cocotb(self):
        return False

    def is_systemc(self):
        return False


class DummyPreprocTestCfg:
    pd = None
    uvm = None

    def __init__(self, script_path):
        self._script_path = script_path

    def get_name(self):
        return "basic"

    def get_builder_name(self):
        return None

    def get_testbench(self):
        return DummyTestbench()

    def get_timeout(self):
        return (1, False)

    def get_plusargs(self):
        return None

    def get_preproc_path(self):
        return self._script_path


def _make_preproc_sim(tmp_path, script_text, *, run_id=None, script_name="preproc.py"):
    script_path = tmp_path / script_name
    script_path.write_text(script_text)
    return VlogSim(
        name="rtl_buddy/vlog_sim",
        root_cfg=DummyRootCfg(),
        test_cfg=DummyPreprocTestCfg(str(script_path)),
        rtl_builder_mode="reg",
        sim_mode={"sim_to_stdout": False},
        suite_dir=str(tmp_path),
        run_id=run_id,
    )


def test_preproc_main_guard_body_does_not_run(tmp_path):
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
    marker = tmp_path / "marker.txt"
    sim = _make_preproc_sim(
        tmp_path,
        f"if __name__ == '__main__':\n    open({str(marker)!r}, 'w').write('ran')\n",
    )

    error = sim.pre()

    assert error is None
    assert not marker.exists()


def test_preproc_else_branch_runs(tmp_path):
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
    marker = tmp_path / "marker.txt"
    sim = _make_preproc_sim(
        tmp_path,
        "if __name__ == '__main__':\n"
        "    pass\n"
        "else:\n"
        f"    open({str(marker)!r}, 'w').write('else-ran')\n",
    )

    error = sim.pre()

    assert error is None
    assert marker.read_text() == "else-ran"


def test_preproc_sees_hook_sentinel_name(tmp_path):
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
    sim = _make_preproc_sim(
        tmp_path,
        "from rtl_buddy.hooks import HOOK_MODULE_NAME\n"
        "assert __name__ == HOOK_MODULE_NAME\n",
    )

    error = sim.pre()

    assert error is None


def test_preproc_plain_module_level_logic_still_runs(tmp_path):
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
    marker = tmp_path / "marker.txt"
    sim = _make_preproc_sim(
        tmp_path, f"open({str(marker)!r}, 'w').write('plain-ran')\n"
    )

    error = sim.pre()

    assert error is None
    assert marker.read_text() == "plain-ran"


# --- preproc namespace: run scoping (issue #415) -----------------------------

_DUMP_NS = (
    "import json, pathlib\n"
    "pathlib.Path(ns_out).write_text(json.dumps({\n"
    "    'run_id': run_id,\n"
    "    'artifact_dir': artifact_dir,\n"
    "    'run_artifact_dir': run_artifact_dir,\n"
    "}))\n"
)


def _preproc_namespace(tmp_path, *, run_id, script_name="preproc.py"):
    import json

    ns_out = tmp_path / f"ns-{run_id}.json"
    sim = _make_preproc_sim(
        tmp_path,
        f"ns_out = {str(ns_out)!r}\n" + _DUMP_NS,
        run_id=run_id,
        script_name=script_name,
    )
    assert sim.pre() is None
    return json.loads(ns_out.read_text())


def test_preproc_namespace_carries_run_id_and_a_run_scoped_dir(tmp_path):
    """A hook can scope its own output to the run it is preparing (#415)."""
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")

    ns = _preproc_namespace(tmp_path, run_id=7)

    assert ns["run_id"] == 7
    assert ns["artifact_dir"] == str(tmp_path / "artefacts" / "basic")
    assert ns["run_artifact_dir"] == str(tmp_path / "artefacts" / "basic" / "run-0007")
    # Handed directories to write into, not paths to mkdir.
    assert Path(ns["artifact_dir"]).is_dir()
    assert Path(ns["run_artifact_dir"]).is_dir()


def test_preproc_run_artifact_dir_is_the_test_dir_without_a_run_id(tmp_path):
    """One pre() serving the whole invocation has nothing to scope apart."""
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")

    ns = _preproc_namespace(tmp_path, run_id=None)

    assert ns["run_id"] is None
    assert ns["run_artifact_dir"] == ns["artifact_dir"]


def test_preproc_run_artifact_dirs_do_not_collide_across_runs(tmp_path):
    """The reported failure: every seed of a randtest shared one output dir.

    `artifact_dir` is test-keyed and stays that way for compatibility, so the
    separation has to come from the run-scoped directory.
    """
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")

    first = _preproc_namespace(tmp_path, run_id=1, script_name="preproc1.py")
    second = _preproc_namespace(tmp_path, run_id=2, script_name="preproc2.py")

    assert first["artifact_dir"] == second["artifact_dir"]
    assert first["run_artifact_dir"] != second["run_artifact_dir"]


def test_preproc_run_artifact_dir_is_where_the_simulation_runs(tmp_path, monkeypatch):
    """The hook's per-run dir must be the sim's cwd, or the sim cannot read it.

    Asserted against the cwd `execute()` actually hands the simulator, not
    against the helper both sides call — otherwise the docs claim "Also the
    simulation's working directory" is not what is being tested.
    """
    from contextlib import nullcontext

    from rtl_buddy.process_utils import ManagedProcessResult
    from rtl_buddy.tools import vlog_sim as vlog_sim_module

    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")

    ns_out = tmp_path / "ns.json"
    sim = _make_preproc_sim(
        tmp_path, f"ns_out = {str(ns_out)!r}\n" + _DUMP_NS, run_id=3
    )
    assert sim.pre() is None
    hook_dir = json.loads(ns_out.read_text())["run_artifact_dir"]

    sim_cwd = {}

    def _fake_run(cmd, *args, cwd=None, **kwargs):
        sim_cwd["cwd"] = cwd
        return ManagedProcessResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _fake_run)
    sim.execute(run_id=3)

    assert sim_cwd["cwd"] == hook_dir


def test_run_multiple_tells_the_hook_it_serves_no_particular_run(tmp_path):
    """One pre() for N runs must not claim to be preparing run 1 (#415).

    `_run_test_cfg_for_run_ids` builds the runner with `run_ids[0]`, so a
    hook defaulting to `self.run_id` would be handed `run-0001` while runs
    2..N simulate elsewhere and never see what it generated. Driven through
    the real `run_multiple`, with the hook raising after it records the
    namespace so the flow stops at PRE.
    """
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")

    ns_out = tmp_path / "ns.json"
    script_path = tmp_path / "preproc.py"
    script_path.write_text(
        f"ns_out = {str(ns_out)!r}\n" + _DUMP_NS + "raise RuntimeError('stop at PRE')\n"
    )
    runner = RtlBuddyTestRunner(
        name="rtl_buddy/testrunner",
        root_cfg=DummyRootCfg(),
        test_cfg=DummyPreprocTestCfg(str(script_path)),
        rtl_builder_mode="reg",
        test_runner_mode={"sim_to_stdout": False},
        suite_dir=str(tmp_path),
        run_id=1,  # what _run_test_cfg_for_run_ids passes: run_ids[0]
    )

    results = runner.run_multiple([1, 2, 3])

    assert len(results) == 3
    ns = json.loads(ns_out.read_text())
    assert ns["run_id"] is None
    assert ns["run_artifact_dir"] == ns["artifact_dir"]


def test_a_single_run_still_gets_its_own_run_id(tmp_path):
    """The `run()` path — plain `test`, or one dispatched element — is the
    case where the hook really is preparing one specific run."""
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")

    ns = _preproc_namespace(tmp_path, run_id=4)

    assert ns["run_id"] == 4
    assert ns["run_artifact_dir"].endswith("run-0004")


# --- sweep (RtlBuddy._expand_tests_with_sweep()) -----------------------------


class DummySweepTest:
    def __init__(self, script_path):
        self.name = "basic"
        self._script_path = script_path

    def get_sweep_path(self):
        return self._script_path

    def get_name(self):
        return self.name


def _make_rb():
    rb = RtlBuddy(name="rtl_buddy")
    rb.builder = "vcs"
    rb.root_cfg = object()
    rb.run_depth = RunDepth.POST
    rb.rtl_builder_mode = "debug"
    return rb


def _run_sweep(tmp_path, script_text):
    script_path = tmp_path / "sweep.py"
    script_path.write_text(script_text)
    return _make_rb()._expand_tests_with_sweep(
        DummySweepTest(str(script_path)), suite_dir=str(tmp_path)
    )


def test_sweep_main_guard_body_does_not_run(tmp_path):
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
    marker = tmp_path / "marker.txt"
    test_cfgs, error = _run_sweep(
        tmp_path,
        "if __name__ == '__main__':\n"
        f"    open({str(marker)!r}, 'w').write('ran')\n"
        "    out_test_cfgs = [test_cfg]\n",
    )

    assert error is None
    assert test_cfgs == []
    assert not marker.exists()


def test_sweep_else_branch_runs(tmp_path):
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
    test_cfgs, error = _run_sweep(
        tmp_path,
        "if __name__ == '__main__':\n"
        "    out_test_cfgs = []\n"
        "else:\n"
        "    out_test_cfgs = [test_cfg]\n",
    )

    assert error is None
    assert len(test_cfgs) == 1
    assert test_cfgs[0].get_name() == "basic"


def test_sweep_sees_hook_sentinel_name(tmp_path):
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
    test_cfgs, error = _run_sweep(
        tmp_path,
        "from rtl_buddy.hooks import HOOK_MODULE_NAME\n"
        "assert __name__ == HOOK_MODULE_NAME\n"
        "out_test_cfgs = [test_cfg]\n",
    )

    assert error is None
    assert len(test_cfgs) == 1
    assert test_cfgs[0].get_name() == "basic"


def test_sweep_plain_module_level_logic_still_runs(tmp_path):
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
    test_cfgs, error = _run_sweep(tmp_path, "out_test_cfgs = [test_cfg]\n")

    assert error is None
    assert len(test_cfgs) == 1
    assert test_cfgs[0].get_name() == "basic"


# --- exec_hook_script: sys.modules registration (issue #343) -----------------


def test_exec_hook_script_registers_module_for_dataclass_hooks(tmp_path):
    """A hook using `from __future__ import annotations` + @dataclass crashes
    with 'NoneType' object has no attribute '__dict__' unless the sentinel
    module is registered in sys.modules during exec — CPython 3.11's
    dataclasses._is_type resolves sys.modules.get(cls.__module__) unguarded."""
    script = tmp_path / "hook.py"
    code = (
        "from __future__ import annotations\n"
        "import dataclasses\n"
        "@dataclasses.dataclass\n"
        "class Vec:\n"
        "    x: int\n"
        "    label: str = ''\n"
        "result = Vec(1).x\n"
    )
    script.write_text(code)

    ns = exec_hook_script(str(script), code, foo="bar")

    assert ns["result"] == 1
    assert ns["foo"] == "bar"
    assert ns["__name__"] == HOOK_MODULE_NAME


def test_exec_hook_script_cleans_sys_modules_on_success_and_raise(tmp_path):
    import sys

    script = tmp_path / "hook.py"
    script.write_text("pass\n")

    exec_hook_script(str(script), "pass\n")
    assert HOOK_MODULE_NAME not in sys.modules

    try:
        exec_hook_script(str(script), "raise RuntimeError('boom')\n")
    except RuntimeError:
        pass
    assert HOOK_MODULE_NAME not in sys.modules


def test_exec_hook_script_restores_previous_sys_modules_binding(tmp_path):
    import sys
    import types

    script = tmp_path / "hook.py"
    script.write_text("pass\n")
    marker = types.ModuleType(HOOK_MODULE_NAME)
    sys.modules[HOOK_MODULE_NAME] = marker
    try:
        exec_hook_script(str(script), "pass\n")
        assert sys.modules[HOOK_MODULE_NAME] is marker
    finally:
        del sys.modules[HOOK_MODULE_NAME]


# --- hook stdout capture (issue #371) ----------------------------------------

# The reproducer from the issue: the template's example_preproc.py prints a
# progress line, which used to arrive on stdout ahead of the envelope.
_PRINTING_PREPROC = (
    "print(f'Running example_preproc.py for test: {test_cfg.get_name()}')\n"
)
_PRINTING_SWEEP = (
    "print(f'Running example_sweep.py for test: {test_cfg.get_name()}')\n"
    "out_test_cfgs = [test_cfg]\n"
)


def test_machine_envelope_parses_with_a_printing_preproc_hook(tmp_path, capsys):
    """The #371 repro: json.loads(stdout) failed at 'line 1 column 1'."""
    setup_logging(color=False, machine=True, log_path=tmp_path / "rtl_buddy.log")
    sim = _make_preproc_sim(tmp_path, _PRINTING_PREPROC)

    assert sim.pre() is None
    RtlBuddy(name="rtl_buddy")._emit_machine_result("test", 0, results=[])

    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["command"] == "test"
    assert envelope["exit_code"] == 0
    assert "Running example_preproc.py" not in captured.out
    # Not dropped — still on stderr, where it cannot corrupt the envelope.
    assert "Running example_preproc.py for test: basic" in captured.err


def test_machine_envelope_parses_with_a_printing_sweep_hook(tmp_path, capsys):
    setup_logging(color=False, machine=True, log_path=tmp_path / "rtl_buddy.log")

    test_cfgs, error = _run_sweep(tmp_path, _PRINTING_SWEEP)
    RtlBuddy(name="rtl_buddy")._emit_machine_result("regression", 0, results=[])

    assert error is None
    assert len(test_cfgs) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["command"] == "regression"
    assert "Running example_sweep.py" not in captured.out
    assert "Running example_sweep.py for test: basic" in captured.err


def test_human_mode_still_shows_the_hook_print(tmp_path, capsys):
    """Human mode must lose no information — the line is re-framed, not hidden.

    The console handler sits at WARNING, so this only holds because the
    capture routes through log_console_event rather than a plain INFO record.
    """
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
    sim = _make_preproc_sim(tmp_path, _PRINTING_PREPROC)

    assert sim.pre() is None

    captured = capsys.readouterr()
    assert "Running example_preproc.py for test: basic" in captured.err
    assert "preproc.py" in captured.err
    assert captured.out == ""


def test_hook_stdout_is_logged_with_stage_and_script(tmp_path):
    """The text stays recoverable from rtl_buddy.log as structured events."""
    log_path = tmp_path / "rtl_buddy.log"
    setup_logging(color=False, machine=True, log_path=log_path)
    script = tmp_path / "hook.py"
    script.write_text("pass\n")

    exec_hook_script(
        str(script),
        "import sys\nprint('first')\nprint()\nsys.stdout.write('trailing')\n",
        stage="preproc",
    )

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    hook_records = [r for r in records if r.get("event") == "hook.stdout"]
    # A partial final line is flushed; a blank line carries nothing.
    assert [r["line"] for r in hook_records] == ["first", "trailing"]
    assert hook_records[0]["stage"] == "preproc"
    assert hook_records[0]["script"] == str(script)


def test_hook_rebinding_sys_stdout_does_not_crash_and_is_restored(tmp_path):
    """Hooks that manage sys.stdout themselves are out of scope, not fatal."""
    import sys

    setup_logging(color=False, machine=True, log_path=tmp_path / "rtl_buddy.log")
    script = tmp_path / "hook.py"
    script.write_text("pass\n")
    outer = sys.stdout

    exec_hook_script(
        str(script),
        "import io, sys\nsys.stdout = io.StringIO()\nprint('hook owned')\n",
        stage="preproc",
    )

    assert sys.stdout is outer


def test_hook_stdout_is_restored_when_the_hook_raises(tmp_path):
    import sys

    setup_logging(color=False, machine=True, log_path=tmp_path / "rtl_buddy.log")
    script = tmp_path / "hook.py"
    script.write_text("pass\n")
    outer = sys.stdout

    try:
        exec_hook_script(
            str(script), "print('before the raise')\nraise RuntimeError('boom')\n"
        )
    except RuntimeError:
        pass

    assert sys.stdout is outer


def test_hook_stdout_is_a_text_sink_not_a_file(tmp_path):
    """The capture is Python-level, and the boundary is deliberate (#371).

    `_HookStdout` is an `io.TextIOBase`, so the two byte-level routes to
    fd 1 — `sys.stdout.fileno()` (as in `subprocess.run(..., stdout=
    sys.stdout)`) and `sys.stdout.buffer` — raise rather than reaching the
    envelope stream. Pinned because it is a behaviour change for hooks that
    used either, and `docs/known-issues.md` promises exactly this shape.
    """
    import io

    setup_logging(color=False, machine=True, log_path=tmp_path / "rtl_buddy.log")
    script = tmp_path / "hook.py"
    script.write_text("pass\n")

    ns = exec_hook_script(
        str(script),
        "import io, sys\n"
        "try:\n"
        "    sys.stdout.fileno()\n"
        "    fileno_error = None\n"
        "except io.UnsupportedOperation as exc:\n"
        "    fileno_error = type(exc).__name__\n"
        "has_buffer = hasattr(sys.stdout, 'buffer')\n",
        stage="preproc",
    )

    assert ns["fileno_error"] == io.UnsupportedOperation.__name__
    assert ns["has_buffer"] is False
