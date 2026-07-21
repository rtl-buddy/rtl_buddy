"""Tests for #338 — prevent cross-suite import collisions in regression
hooks.

``VlogSim._check_preproc_imports(ns, script_path)`` inspects a preproc's
namespace after exec for imported module objects whose ``__file__`` lives
under the project root but in a *different* suite directory (a directory
containing ``tests.yaml``) than the preproc script's own suite directory.
This catches the scenario where ``suite_b``'s ``import replay_io`` returns
``suite_a``'s cached module (a ``sys.modules`` caching collision).

Constructing a full ``VlogSim`` needs a real ``TestConfig``/``RootConfig``,
so this is exercised as a focused unit test via ``VlogSim.__new__``, with
only the two attributes ``_check_preproc_imports``/``_find_suite_dir``
actually touch: ``root_cfg`` and ``test_name``. The end-to-end regression
scenario (two suites, same-named preproc helper, real sim run) is covered
structurally by this unit test — the stub ``echo`` builder used elsewhere
in this suite can't exercise a real preproc import at all, so there is no
value in also attempting it end-to-end.
"""

from __future__ import annotations

import os
import types
from pathlib import Path

from rtl_buddy.tools.vlog_sim import VlogSim


class _StubRootCfg:
    def __init__(self, root: Path):
        self._root = str(root)

    def get_project_rootdir(self):
        return self._root


def _make_vlog_sim(root: Path) -> VlogSim:
    vs = VlogSim.__new__(VlogSim)
    vs.root_cfg = _StubRootCfg(root)
    vs.test_name = "t"
    return vs


def _make_module(name: str, file_path: Path) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__file__ = str(file_path)
    return mod


def test_import_from_different_suite_directory_is_flagged(tmp_path: Path):
    suite_a = tmp_path / "suite_a"
    suite_b = tmp_path / "suite_b"
    suite_a.mkdir()
    suite_b.mkdir()
    (suite_a / "tests.yaml").write_text("")
    (suite_b / "tests.yaml").write_text("")
    helper = suite_a / "replay_io.py"
    helper.write_text("# helper\n")

    vs = _make_vlog_sim(tmp_path)
    ns = {"replay_io": _make_module("replay_io", helper)}
    script_path = str(suite_b / "preproc.py")

    error = vs._check_preproc_imports(ns, script_path)

    assert error is not None
    assert "different suite" in error


def test_import_from_same_suite_directory_is_not_flagged(tmp_path: Path):
    suite_b = tmp_path / "suite_b"
    suite_b.mkdir()
    (suite_b / "tests.yaml").write_text("")
    helper = suite_b / "replay_io.py"
    helper.write_text("# helper\n")

    vs = _make_vlog_sim(tmp_path)
    ns = {"replay_io": _make_module("replay_io", helper)}
    script_path = str(suite_b / "preproc.py")

    assert vs._check_preproc_imports(ns, script_path) is None


def test_stdlib_import_is_not_flagged(tmp_path: Path):
    suite_b = tmp_path / "suite_b"
    suite_b.mkdir()
    (suite_b / "tests.yaml").write_text("")

    vs = _make_vlog_sim(tmp_path)
    ns = {"os": os}
    script_path = str(suite_b / "preproc.py")

    assert vs._check_preproc_imports(ns, script_path) is None


def test_import_collision_has_dedicated_human_message():
    """The preproc.import_collision ERROR event renders a specific message,
    not the lossy dotted-event fallback."""
    from rtl_buddy.logging_utils import _human_message

    msg = _human_message(
        "preproc.import_collision",
        {"test": "t", "script": "verif/suite_b/preproc.py", "error": "boom"},
    )
    assert msg != "preproc import_collision"
    assert "t" in msg
    assert "boom" in msg
