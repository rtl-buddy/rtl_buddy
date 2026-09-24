"""`compile-time` tokens get variable expansion and a project-root spelling (#659).

The compile runs from the test's artefact directory, which `--run-tag` moves
one level deeper, so a relative path in a builder mode's `compile-time` named
a different file per layout. These tests pin the expansion and that the
project-root variable is rtl_buddy's own, not the caller's; the compile line
and shared-build key are covered in test_shared_build.py.
"""

from rtl_buddy.config.rtl import expand_compile_opts


def test_project_root_variable_expands_in_both_spellings():
    opts = ["${RTL_BUDDY_PROJECT_ROOT}/a.vlt", "$RTL_BUDDY_PROJECT_ROOT/b.vlt"]
    assert expand_compile_opts(opts, "/proj") == ["/proj/a.vlt", "/proj/b.vlt"]


def test_project_root_variable_ignores_the_callers_environment(monkeypatch):
    monkeypatch.setenv("RTL_BUDDY_PROJECT_ROOT", "/elsewhere")
    assert expand_compile_opts(["${RTL_BUDDY_PROJECT_ROOT}/a.vlt"], "/proj") == [
        "/proj/a.vlt"
    ]


def test_a_longer_name_sharing_the_prefix_is_not_the_project_root(monkeypatch):
    monkeypatch.setenv("RTL_BUDDY_PROJECT_ROOTS", "/other")
    assert expand_compile_opts(["$RTL_BUDDY_PROJECT_ROOTS/x"], "/proj") == ["/other/x"]


def test_environment_variables_and_home_expand(monkeypatch):
    monkeypatch.setenv("MY_PROJECT_ROOT", "/checkout")
    monkeypatch.setenv("HOME", "/home/me")
    opts = ["--binary", "${MY_PROJECT_ROOT}/waive.vlt", "~/lib.vlt"]
    assert expand_compile_opts(opts, "/proj") == [
        "--binary",
        "/checkout/waive.vlt",
        "/home/me/lib.vlt",
    ]


def test_an_unset_variable_is_left_as_written(monkeypatch):
    # POSIX expandvars leaves it, so a compiler that expands it still can.
    monkeypatch.delenv("RB_TEST_UNSET_VAR", raising=False)
    assert expand_compile_opts(["${RB_TEST_UNSET_VAR}/x"], "/proj") == [
        "${RB_TEST_UNSET_VAR}/x"
    ]


def test_without_a_project_root_the_variable_comes_from_the_environment(
    monkeypatch,
):
    monkeypatch.delenv("RTL_BUDDY_PROJECT_ROOT", raising=False)
    assert expand_compile_opts(["${RTL_BUDDY_PROJECT_ROOT}/x"], None) == [
        "${RTL_BUDDY_PROJECT_ROOT}/x"
    ]


def test_plain_tokens_pass_through_unchanged():
    opts = ["--binary", "-o", "simv", "+define+X=1", "-CFLAGS", "-DY=$(Z)"]
    assert expand_compile_opts(opts, "/proj") == opts
