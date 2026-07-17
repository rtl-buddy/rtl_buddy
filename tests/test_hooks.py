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

from rtl_buddy.hooks import HOOK_MODULE_NAME, build_hook_namespace
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


def _make_preproc_sim(tmp_path, script_text):
    script_path = tmp_path / "preproc.py"
    script_path.write_text(script_text)
    return VlogSim(
        name="rtl_buddy/vlog_sim",
        root_cfg=DummyRootCfg(),
        test_cfg=DummyPreprocTestCfg(str(script_path)),
        rtl_builder_mode="reg",
        sim_mode={"sim_to_stdout": False},
        suite_dir=str(tmp_path),
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
