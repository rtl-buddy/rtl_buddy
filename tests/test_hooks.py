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
"""

from pathlib import Path

from rtl_buddy.hooks import HOOK_MODULE_NAME, build_hook_namespace, exec_hook_script
from rtl_buddy.logging_utils import setup_logging
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.test_runner import RunDepth
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


class DummyTestbench:
    def get_filelist(self):
        return []


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


def test_preproc_run_artifact_dir_is_where_the_simulation_runs(tmp_path):
    """The hook's per-run dir must be the sim's cwd, or the sim cannot read it."""
    setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")

    sim = _make_preproc_sim(tmp_path, "pass\n", run_id=3)

    assert sim._ensure_artifact_dir(run_id=sim.run_id) == str(
        tmp_path / "artefacts" / "basic" / "run-0003"
    )


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
