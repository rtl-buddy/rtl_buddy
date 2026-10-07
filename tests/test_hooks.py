# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Tests for the hook ``exec()`` namespace and hook stdout capture.

Hook scripts (`preproc`, `sweep`) run in a hand-built namespace whose ``__name__``
is the ``HOOK_MODULE_NAME`` sentinel, never ``"__main__"``. The tests cover both
exec sites and the helper, then a hook's own ``print()``, which must stay off
stdout because under ``--machine`` stdout is the envelope stream.
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


# preproc (VlogSim.pre())


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


# preproc namespace: run scoping

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
    """A hook can scope its output to the run it is preparing."""
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
    """Run-scoped artifact dirs do not collide across runs.

    `artifact_dir` stays test-keyed, so the separation comes from the run-scoped directory.
    """
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")

    first = _preproc_namespace(tmp_path, run_id=1, script_name="preproc1.py")
    second = _preproc_namespace(tmp_path, run_id=2, script_name="preproc2.py")

    assert first["artifact_dir"] == second["artifact_dir"]
    assert first["run_artifact_dir"] != second["run_artifact_dir"]


def test_preproc_run_artifact_dir_is_where_the_simulation_runs(tmp_path, monkeypatch):
    """The hook's per-run dir is the sim's cwd, asserted against the cwd `execute()`
    hands the simulator."""
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
    """`run_multiple` tells the hook it serves no particular run.

    `_run_test_cfg_for_run_ids` builds the runner with `run_ids[0]`, so a hook
    defaulting to `self.run_id` would get `run-0001` for all N runs. The hook raises
    after recording the namespace so the flow stops at PRE.
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
    """A single `run()` gives the hook its own run id."""
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")

    ns = _preproc_namespace(tmp_path, run_id=4)

    assert ns["run_id"] == 4
    assert ns["run_artifact_dir"].endswith("run-0004")


# sweep (RtlBuddy._expand_tests_with_sweep())


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


# exec_hook_script: sys.modules registration


def test_exec_hook_script_registers_module_for_dataclass_hooks(tmp_path):
    """The sentinel module is registered in sys.modules during exec, or `from __future__
    import annotations` + @dataclass hooks crash in `dataclasses._is_type`."""
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


# hook stdout capture

# The template's example_preproc.py prints a progress line that must not precede the
# envelope.
_PRINTING_PREPROC = (
    "print(f'Running example_preproc.py for test: {test_cfg.get_name()}')\n"
)
_PRINTING_SWEEP = (
    "print(f'Running example_sweep.py for test: {test_cfg.get_name()}')\n"
    "out_test_cfgs = [test_cfg]\n"
)


def test_machine_envelope_parses_with_a_printing_preproc_hook(tmp_path, capsys):
    """The machine envelope parses with a printing preproc hook."""
    setup_logging(color=False, machine=True, log_path=tmp_path / "rtl_buddy.log")
    sim = _make_preproc_sim(tmp_path, _PRINTING_PREPROC)

    assert sim.pre() is None
    RtlBuddy(name="rtl_buddy")._emit_machine_result("test", 0, results=[])

    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["command"] == "test"
    assert envelope["exit_code"] == 0
    assert "Running example_preproc.py" not in captured.out
    # Not dropped: still on stderr.
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
    """Human mode still shows the hook print, re-framed.

    The console handler sits at WARNING, so this holds only because the capture uses
    log_console_event.
    """
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
    sim = _make_preproc_sim(tmp_path, _PRINTING_PREPROC)

    assert sim.pre() is None

    captured = capsys.readouterr()
    assert "Running example_preproc.py for test: basic" in captured.err
    assert "preproc.py" in captured.err
    assert captured.out == ""


def test_hook_stdout_is_logged_with_stage_and_script(tmp_path):
    """Hook stdout is logged as structured events with stage and script."""
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
    """A hook rebinding sys.stdout does not crash and stdout is restored."""
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
    """Hook stdout is a text sink: `sys.stdout.fileno()` and `sys.stdout.buffer` raise
    (`docs/known-issues.md`)."""
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


# root_cfg is read-only in hooks (#14)


def test_preproc_reads_root_cfg(tmp_path):
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
    out = tmp_path / "out.txt"
    sim = _make_preproc_sim(
        tmp_path,
        f"open({str(out)!r}, 'w').write(str(root_cfg.get_use_lcov('verilator')))\n",
    )

    assert sim.pre() is None
    assert out.read_text() == "False"


def test_preproc_cannot_rebind_a_root_cfg_attribute(tmp_path):
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
    sim = _make_preproc_sim(tmp_path, "root_cfg.builder_override = 'vcs'\n")

    error = sim.pre()

    assert error is not None and "root_cfg is read-only" in error
    assert not hasattr(sim.root_cfg, "builder_override")


def test_preproc_cannot_delete_a_root_cfg_attribute(tmp_path):
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
    sim = _make_preproc_sim(tmp_path, "del root_cfg.get_use_lcov\n")

    error = sim.pre()

    assert error is not None and "root_cfg is read-only" in error


def test_sweep_cannot_rebind_a_root_cfg_attribute(tmp_path):
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")

    class Cfg:
        builder_override = None

    rb = _make_rb()
    rb.root_cfg = Cfg()
    script_path = tmp_path / "sweep.py"
    script_path.write_text("root_cfg.builder_override = 'vcs'\nout_test_cfgs = []\n")

    test_cfgs, error = rb._expand_tests_with_sweep(
        DummySweepTest(str(script_path)), suite_dir=str(tmp_path)
    )

    assert error is not None and "root_cfg is read-only" in str(error)
    assert rb.root_cfg.builder_override is None
