"""`abc-persistent: false` runs Yosys ABC in batch mode through an `abc-batch` link (#826)."""

import logging
import os
from pathlib import Path

import pytest
from serde.yaml import from_yaml

from rtl_buddy.config.synth import (
    SynthToolConfig,
    SynthToolConfigFile,
    SynthToolOpts,
    SynthToolOptsFile,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools.synth_yosys import (
    ABC_BATCH_LINK_NAME,
    YOSYS_TMP_DIRNAME,
    abc_batch_cmds,
    resolve_yosys_abc,
)

from test_abc_constr import _options
from test_clock_gating import BACKENDS, _synth

BATCH = SynthToolOptsFile(abc_persistent=False)


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def yosys_bin(tmp_path, monkeypatch):
    """A `bin/` holding `yosys` and `yosys-abc`, first on PATH, so `tool: yosys` resolves there."""
    bin_dir = tmp_path / "bin"
    _exe(bin_dir / "yosys")
    abc = _exe(bin_dir / "yosys-abc")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return abc


def _link(tmp_path, backend) -> Path:
    return (
        tmp_path
        / "artefacts"
        / f"g_{backend}"
        / YOSYS_TMP_DIRNAME
        / ABC_BATCH_LINK_NAME
    )


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("mapped", [True, False])
def test_persistent_by_default(tmp_path, backend, mapped, yosys_bin):
    synth, script, _lib = _synth(tmp_path, backend, mapped=mapped)
    assert "abc.exe" not in script
    assert not os.path.lexists(_link(tmp_path, backend))
    assert "abc_persistent" not in _options(synth, backend)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("mapped", [True, False])
def test_batch_points_abc_exe_at_a_link_to_yosys_abc(
    tmp_path, backend, mapped, yosys_bin
):
    synth, script, _lib = _synth(tmp_path, backend, tool_opts=BATCH, mapped=mapped)
    link = _link(tmp_path, backend)
    # First line, so `synth`'s own `abc -fast` sees it as well as the mapped `abc`.
    assert script.splitlines()[0] == f"scratchpad -set abc.exe {link}"
    assert link.is_symlink()
    assert os.readlink(link) == str(yosys_bin)
    assert _options(synth, backend)["abc_persistent"] is False


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_override_wins_over_the_tool_opts(tmp_path, backend, yosys_bin):
    _s, script, _lib = _synth(
        tmp_path, backend, tool_opts=BATCH, overrides={"abc_persistent": True}
    )
    assert "abc.exe" not in script
    _s, script, _lib = _synth(tmp_path, backend, overrides={"abc_persistent": False})
    assert "scratchpad -set abc.exe" in script


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_default_digest_is_unchanged(tmp_path, backend, yosys_bin):
    """An explicit `abc-persistent: true` digests exactly as an unset one."""
    for sub in "abc":
        (tmp_path / sub).mkdir()
    default, _script, _lib = _synth(tmp_path / "a", backend)
    explicit, _script, _lib = _synth(
        tmp_path / "b", backend, tool_opts=SynthToolOptsFile(abc_persistent=True)
    )
    batch, _script, _lib = _synth(tmp_path / "c", backend, tool_opts=BATCH)

    def strip(options):
        # The library fingerprint names each run's own tmp_path.
        return {k: v for k, v in options.items() if k != "libs"}

    assert strip(_options(default, backend)) == strip(_options(explicit, backend))
    assert strip(_options(batch, backend)) != strip(_options(default, backend))


def test_switching_back_removes_the_stale_link(tmp_path, yosys_bin):
    _synth(tmp_path, "yosys", tool_opts=BATCH)
    assert _link(tmp_path, "yosys").is_symlink()
    _synth(tmp_path, "yosys")
    assert not os.path.lexists(_link(tmp_path, "yosys"))


def test_a_rerun_replaces_the_link(tmp_path, yosys_bin):
    link = tmp_path / "art" / YOSYS_TMP_DIRNAME / ABC_BATCH_LINK_NAME
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "old-yosys-abc")
    opts = SynthToolOpts(abc_persistent=False)
    assert abc_batch_cmds(opts, "yosys", str(tmp_path / "art")) == [
        f"scratchpad -set abc.exe {link}"
    ]
    assert os.readlink(link) == str(yosys_bin)


def test_yosys_abc_is_found_next_to_the_resolved_yosys(tmp_path, monkeypatch):
    """Yosys looks beside its real binary, so a symlinked `yosys` resolves to that directory's `yosys-abc`."""
    real = _exe(tmp_path / "install" / "bin" / "yosys")
    abc = _exe(tmp_path / "install" / "bin" / "yosys-abc")
    shim = tmp_path / "shims"
    shim.mkdir()
    (shim / "yosys").symlink_to(real)
    _exe(tmp_path / "elsewhere" / "yosys-abc")
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    assert resolve_yosys_abc(str(shim / "yosys")) == str(abc)


def test_yosys_abc_falls_back_to_path(tmp_path, monkeypatch):
    yosys = _exe(tmp_path / "a" / "yosys")
    abc = _exe(tmp_path / "b" / "yosys-abc")
    monkeypatch.setenv("PATH", str(tmp_path / "b"))
    assert resolve_yosys_abc(str(yosys)) == str(abc)


def test_a_missing_yosys_abc_is_a_config_error(tmp_path, monkeypatch, caplog):
    yosys = _exe(tmp_path / "a" / "yosys")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with caplog.at_level(logging.ERROR):
        with pytest.raises(FatalRtlBuddyError, match="abc-persistent: false"):
            abc_batch_cmds(
                SynthToolOpts(abc_persistent=False), str(yosys), str(tmp_path / "art")
            )
    assert any(
        getattr(r, "rtl_event", None) == "synth.yosys_abc_missing"
        for r in caplog.records
    )
    assert not (tmp_path / "art").exists()


def test_the_key_loads_from_yaml():
    assert from_yaml(SynthToolOptsFile, "{}\n").abc_persistent is True
    opts = from_yaml(SynthToolOptsFile, "abc-persistent: false\n")
    assert opts.abc_persistent is False


def test_a_quoted_override_is_a_config_error():
    cfg = SynthToolConfig(SynthToolConfigFile(name="yosys", tool="yosys"))
    with pytest.raises(FatalRtlBuddyError, match="abc_persistent must be a bool"):
        cfg.get_opts({"abc_persistent": "false"})
