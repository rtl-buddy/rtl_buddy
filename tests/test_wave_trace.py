import os

import pytest

from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.process_utils import ManagedProcessResult
from rtl_buddy.tools import wave_launcher as wave_launcher_module
from rtl_buddy.tools.wave_launcher import prepare_surfer_trace
from rtl_buddy.tools.wave_trace import TRACE_CANDIDATES, newest_trace


def _touch(path, mtime=None):
    with open(path, "w") as fp:
        fp.write("x")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


# ---------------------------------------------------------------------------
# newest_trace candidate resolution
# ---------------------------------------------------------------------------


def test_newest_trace_none_when_empty(tmp_path):
    assert newest_trace(str(tmp_path)) is None


def test_newest_trace_returns_fst_when_only_fst(tmp_path):
    _touch(tmp_path / "dump.fst")
    assert newest_trace(str(tmp_path)) == str(tmp_path / "dump.fst")


def test_newest_trace_falls_back_to_vcd(tmp_path):
    # The default Icarus path: only a VCD is present, no FST.
    _touch(tmp_path / "dump.vcd")
    assert newest_trace(str(tmp_path)) == str(tmp_path / "dump.vcd")


def test_newest_trace_picks_newest_mtime(tmp_path):
    # Both present — follow whichever builder ran last (newest mtime wins).
    _touch(tmp_path / "dump.fst", mtime=1000)
    _touch(tmp_path / "dump.vcd", mtime=2000)
    assert newest_trace(str(tmp_path)) == str(tmp_path / "dump.vcd")

    os.utime(tmp_path / "dump.fst", (3000, 3000))
    assert newest_trace(str(tmp_path)) == str(tmp_path / "dump.fst")


def test_trace_candidates_shared_with_axi_profiler():
    from rtl_buddy.tools.axi_profile_rtl_buddy import RtlBuddyAxiProfileRun

    assert RtlBuddyAxiProfileRun._TRACE_CANDIDATES is TRACE_CANDIDATES
    assert TRACE_CANDIDATES == ("dump.fst", "dump.vcd", "vcdplus.vpd")


# ---------------------------------------------------------------------------
# prepare_surfer_trace
# ---------------------------------------------------------------------------


def test_prepare_passes_fst_through(tmp_path):
    fst = str(tmp_path / "dump.fst")
    _touch(tmp_path / "dump.fst")
    assert prepare_surfer_trace(fst, None, "basic") == fst


def test_prepare_passes_vcd_through_without_postproc(tmp_path):
    vcd = str(tmp_path / "dump.vcd")
    _touch(tmp_path / "dump.vcd")
    # Surfer reads VCD natively, so no conversion when wave_format is unset.
    assert prepare_surfer_trace(vcd, None, "basic") == vcd


def test_prepare_rejects_vpd(tmp_path):
    vpd = str(tmp_path / "vcdplus.vpd")
    _touch(tmp_path / "vcdplus.vpd")
    with pytest.raises(FatalRtlBuddyError, match="VCS VPD"):
        prepare_surfer_trace(vpd, None, "basic")


def test_prepare_fst_postproc_converts_vcd(tmp_path, monkeypatch):
    vcd = str(tmp_path / "dump.vcd")
    fst = str(tmp_path / "dump.fst")
    _touch(tmp_path / "dump.vcd")

    monkeypatch.setattr(
        wave_launcher_module.shutil, "which", lambda _: "/usr/bin/vcd2fst"
    )

    def _fake_run(cmd, **kwargs):
        assert cmd == ["vcd2fst", vcd, fst]
        _touch(tmp_path / "dump.fst")
        return ManagedProcessResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wave_launcher_module, "run_managed_process", _fake_run)
    monkeypatch.setattr(wave_launcher_module, "task_status", lambda *a, **k: _nullctx())

    assert prepare_surfer_trace(vcd, "fst-postproc", "basic") == fst


def test_prepare_fst_postproc_falls_back_when_tool_missing(tmp_path, monkeypatch):
    vcd = str(tmp_path / "dump.vcd")
    _touch(tmp_path / "dump.vcd")
    monkeypatch.setattr(wave_launcher_module.shutil, "which", lambda _: None)
    # No vcd2fst on PATH → return the VCD unchanged (Surfer reads it anyway).
    assert prepare_surfer_trace(vcd, "fst-postproc", "basic") == vcd


def test_prepare_fst_postproc_uses_cache(tmp_path, monkeypatch):
    vcd = str(tmp_path / "dump.vcd")
    fst = str(tmp_path / "dump.fst")
    _touch(tmp_path / "dump.vcd", mtime=1000)
    _touch(tmp_path / "dump.fst", mtime=2000)  # fst newer than vcd → cached

    def _boom(*a, **k):
        raise AssertionError("vcd2fst should not run when cache is fresh")

    monkeypatch.setattr(wave_launcher_module, "run_managed_process", _boom)
    monkeypatch.setattr(
        wave_launcher_module.shutil, "which", lambda _: "/usr/bin/vcd2fst"
    )
    assert prepare_surfer_trace(vcd, "fst-postproc", "basic") == fst


class _nullctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
