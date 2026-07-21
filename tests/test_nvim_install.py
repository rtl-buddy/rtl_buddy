# rtl-buddy
#
# Hermetic tests for tools/nvim_install.py. git is never actually invoked:
# subprocess.run and shutil.which are stubbed, and $HOME is repointed at a
# tmp dir so the managed setup file / pack dir land under the test sandbox.
import types

import pytest

from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import nvim_install


@pytest.fixture
def git_env(tmp_path, monkeypatch):
    """Repoint $HOME, stub `git`, and record every git invocation.

    Yields the list of command-arg lists passed to subprocess.run so tests can
    assert on the constructed git commands. The stub reports success and does
    NOT create the pack dir (so each test controls pack-dir existence itself).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(nvim_install._ENV_SOURCE, raising=False)
    monkeypatch.delenv(nvim_install._ENV_REF, raising=False)
    monkeypatch.setattr(nvim_install.shutil, "which", lambda _name: "/usr/bin/git")

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(
            returncode=0, stdout="", stderr="", timed_out=False
        )

    monkeypatch.setattr(nvim_install, "run_managed_process", fake_run)
    return calls


def _git_calls(calls):
    """Map recorded commands to their git subcommand verb for easy assertions."""
    verbs = []
    for cmd in calls:
        # cmd == ["/usr/bin/git", "<verb>", ...]  or  [..., "-C", dir, "<verb>", ...]
        if "-C" in cmd:
            verbs.append(cmd[cmd.index("-C") + 2])
        else:
            verbs.append(cmd[1])
    return verbs


def test_fresh_install_clones_pinned_revision(git_env):
    nvim_install.install()

    assert _git_calls(git_env) == ["clone"]
    clone = git_env[0]
    assert clone[:5] == ["/usr/bin/git", "clone", "--depth", "1", "--branch"]
    assert clone[5] == nvim_install.RTL_BUDDY_NVIM_REF
    assert clone[6] == nvim_install.RTL_BUDDY_NVIM_REPO
    assert clone[7] == str(nvim_install.pack_dir())

    setup = nvim_install.setup_file()
    assert setup.is_file()
    body = setup.read_text()
    assert 'require("rtlbuddy")' in body
    assert "auto_connect = true" in body
    assert "wave = { annotate = true }" in body


def test_force_removes_existing_then_reclones(git_env):
    pack = nvim_install.pack_dir()
    pack.mkdir(parents=True)
    (pack / "marker").write_text("old")

    nvim_install.install(force=True)

    assert not (pack / "marker").exists()  # rmtree happened
    assert _git_calls(git_env) == ["clone"]


def test_update_fetches_and_resets_when_git_repo(git_env):
    pack = nvim_install.pack_dir()
    (pack / ".git").mkdir(parents=True)

    nvim_install.install(update=True)

    assert _git_calls(git_env) == ["fetch", "reset"]
    fetch, reset = git_env
    assert fetch[:3] == ["/usr/bin/git", "-C", str(pack)]
    assert fetch[3:5] == ["fetch", "--depth"]
    assert nvim_install.RTL_BUDDY_NVIM_REF in fetch
    assert reset[-3:] == ["reset", "--hard", "FETCH_HEAD"]


def test_update_reclones_when_not_a_git_repo(git_env):
    pack = nvim_install.pack_dir()
    pack.mkdir(parents=True)  # exists but no .git

    nvim_install.install(update=True)

    assert _git_calls(git_env) == ["clone"]


def test_already_installed_without_flags_skips_git_but_writes_setup(git_env):
    pack = nvim_install.pack_dir()
    pack.mkdir(parents=True)

    nvim_install.install()

    assert git_env == []  # no clone/fetch
    assert nvim_install.setup_file().is_file()  # setup still (re)written


def test_source_and_ref_overrides(git_env):
    nvim_install.install(source="/local/rtl-buddy-nvim", ref="feat/branch")

    clone = git_env[0]
    assert clone[5] == "feat/branch"
    assert clone[6] == "/local/rtl-buddy-nvim"


def test_env_overrides(git_env, monkeypatch):
    monkeypatch.setenv(nvim_install._ENV_SOURCE, "/env/path")
    monkeypatch.setenv(nvim_install._ENV_REF, "env-ref")

    nvim_install.install()

    clone = git_env[0]
    assert clone[5] == "env-ref"
    assert clone[6] == "/env/path"


def test_explicit_args_win_over_env(git_env, monkeypatch):
    monkeypatch.setenv(nvim_install._ENV_SOURCE, "/env/path")
    monkeypatch.setenv(nvim_install._ENV_REF, "env-ref")

    nvim_install.install(source="/explicit", ref="explicit-ref")

    clone = git_env[0]
    assert clone[5] == "explicit-ref"
    assert clone[6] == "/explicit"


def test_no_lsp_omits_verible_autostart(git_env):
    nvim_install.install(lsp=False)
    body = nvim_install.setup_file().read_text()
    assert "verible-verilog-ls" not in body
    assert "auto_connect = true" in body


def test_legacy_plugin_removed_on_install(git_env):
    legacy = nvim_install.legacy_plugin_file()
    legacy.parent.mkdir(parents=True)
    legacy.write_text("-- old annotation plugin")

    nvim_install.install()

    assert not legacy.exists()


def test_git_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(nvim_install.shutil, "which", lambda _name: None)
    with pytest.raises(FatalRtlBuddyError, match="git"):
        nvim_install.install()


def test_git_failure_raises_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(nvim_install.shutil, "which", lambda _name: "/usr/bin/git")

    def failing_run(cmd, *args, **kwargs):
        return types.SimpleNamespace(
            returncode=128, stdout="", stderr="fatal: not found", timed_out=False
        )

    monkeypatch.setattr(nvim_install, "run_managed_process", failing_run)
    with pytest.raises(FatalRtlBuddyError, match="not found"):
        nvim_install.install()


def test_git_timeout_raises_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(nvim_install.shutil, "which", lambda _name: "/usr/bin/git")

    def timed_out_run(cmd, *args, **kwargs):
        return types.SimpleNamespace(
            returncode=None, stdout="", stderr="", timed_out=True
        )

    monkeypatch.setattr(nvim_install, "run_managed_process", timed_out_run)
    with pytest.raises(FatalRtlBuddyError, match="timed out"):
        nvim_install.install()


def test_pin_tracks_hub_protocol_version():
    """Tripwire: the pinned rtl-buddy-nvim ref is vetted against this hub
    PROTOCOL_VERSION. If the hub bumps the protocol, this fails in CI so the
    maintainer re-pins (tag a compatible release + bump RTL_BUDDY_NVIM_REF and
    _PIN_PROTOCOL_VERSION) instead of letting it surface at a user's handshake.
    """
    from rtl_buddy.hub.protocol import PROTOCOL_VERSION

    assert nvim_install._PIN_PROTOCOL_VERSION == PROTOCOL_VERSION, (
        "hub PROTOCOL_VERSION changed without re-vetting RTL_BUDDY_NVIM_REF — tag a "
        "compatible rtl-buddy-nvim release, bump RTL_BUDDY_NVIM_REF to it, and bump "
        "_PIN_PROTOCOL_VERSION. See docs/known-issues.md."
    )
