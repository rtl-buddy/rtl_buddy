"""Tests for the macOS LaunchAgent integration (issue #122).

The render path is platform-independent and exercised everywhere
(it's pure XML string construction). The install/uninstall paths
are macOS-only — Linux CI runs them through the
:class:`LaunchAgentUnsupportedError` guard, the macOS path is
covered with ``platform``-patched fakes plus a captured
``launchctl`` so we exercise the real flow without depending on
``launchctl`` being on PATH (the test machine often won't have it).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from rtl_buddy.hub import launchagent
from rtl_buddy.hub.launchagent import (
    LABEL,
    LaunchAgentError,
    LaunchAgentUnsupportedError,
    install,
    is_supported,
    render_plist,
    uninstall,
)


# --- render path (platform-independent) -------------------------------------


def test_render_plist_is_valid_xml(tmp_path: Path):
    """The generated XML must parse cleanly and carry the canonical
    LaunchAgent keys. Standin for ``plutil -lint`` which isn't
    guaranteed on every CI host."""
    xml = render_plist(
        python="/usr/bin/python3",
        project_root=tmp_path,
        log_path=tmp_path / "hub.log",
    )
    root = ET.fromstring(xml)  # noqa: S314 — fixed-content rendered locally
    assert root.tag == "plist"
    keys = [el.text for el in root.iter("key")]
    for required in (
        "Label",
        "ProgramArguments",
        "WorkingDirectory",
        "RunAtLoad",
        "KeepAlive",
        "StandardOutPath",
        "StandardErrorPath",
    ):
        assert required in keys, f"missing required key: {required}"


def test_render_plist_label_matches_constant(tmp_path: Path):
    xml = render_plist(project_root=tmp_path)
    assert f"<string>{LABEL}</string>" in xml


def test_render_plist_program_args_run_rb_hub_start(tmp_path: Path):
    xml = render_plist(python="/opt/homebrew/bin/python3.13", project_root=tmp_path)
    root = ET.fromstring(xml)  # noqa: S314
    # Find ProgramArguments → array → string list.
    args: list[str] = []
    for el in root.iter("dict"):
        children = list(el)
        for i, child in enumerate(children):
            if (
                child.tag == "key"
                and child.text == "ProgramArguments"
                and i + 1 < len(children)
                and children[i + 1].tag == "array"
            ):
                args = [s.text or "" for s in children[i + 1].iter("string")]
                break
    assert args == [
        "/opt/homebrew/bin/python3.13",
        "-m",
        "rtl_buddy",
        "hub",
        "start",
        "--foreground",
    ]


def test_render_plist_escapes_xml_special_chars(tmp_path: Path):
    """A project path with an ``&`` should not break the XML."""
    weird = tmp_path / "ampersand & quote"
    weird.mkdir()
    xml = render_plist(project_root=weird)
    # If the escape worked, ET.fromstring round-trips.
    root = ET.fromstring(xml)  # noqa: S314
    # Find the WorkingDirectory string sibling.
    for el in root.iter("dict"):
        children = list(el)
        for i, child in enumerate(children):
            if (
                child.tag == "key"
                and child.text == "WorkingDirectory"
                and i + 1 < len(children)
            ):
                assert "&" in children[i + 1].text  # round-trip restored
                return
    pytest.fail("WorkingDirectory key not found in plist")


def test_render_plist_default_log_path_under_rtl_buddy_dir(tmp_path: Path):
    xml = render_plist(project_root=tmp_path)
    expected = str((tmp_path / ".rtl-buddy" / "hub.log").resolve())
    assert expected in xml


# --- install / uninstall on non-macOS ---------------------------------------


@pytest.mark.skipif(is_supported(), reason="runs on non-macOS hosts only")
def test_install_on_non_macos_raises_unsupported():
    with pytest.raises(LaunchAgentUnsupportedError, match="macOS-only"):
        install()


@pytest.mark.skipif(is_supported(), reason="runs on non-macOS hosts only")
def test_uninstall_on_non_macos_raises_unsupported():
    with pytest.raises(LaunchAgentUnsupportedError, match="macOS-only"):
        uninstall()


# --- install / uninstall on simulated macOS ---------------------------------


def _force_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the test host is macOS so the install path exercises.

    Two surfaces gate macOS-only behaviour: :func:`is_supported`
    (consulted by ``install`` / ``uninstall``) and any direct read
    of :data:`sys.platform`. Patch both.
    """
    monkeypatch.setattr(launchagent, "is_supported", lambda: True)
    monkeypatch.setattr("sys.platform", "darwin")


def test_install_writes_plist_and_loads_via_launchctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _force_macos(monkeypatch)
    plist_path = tmp_path / "agents" / "com.rtl-buddy.hub.plist"
    calls: list[list[str]] = []

    fake_launchctl = tmp_path / "fake-launchctl"
    fake_launchctl.write_text("#!/bin/sh\nexit 0\n")
    fake_launchctl.chmod(0o755)

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    target = install(
        project_root=tmp_path,
        plist_path=plist_path,
        launchctl=str(fake_launchctl),
    )

    assert target == plist_path
    assert plist_path.exists()
    assert "<plist" in plist_path.read_text()
    # The install should have run a single ``load`` (no prior
    # plist → no preliminary ``unload``).
    assert calls == [[str(fake_launchctl), "load", str(plist_path)]]


def test_install_unloads_prior_plist_before_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _force_macos(monkeypatch)
    plist_path = tmp_path / "com.rtl-buddy.hub.plist"
    plist_path.write_text("<?xml ?><plist></plist>")  # prior install present
    calls: list[list[str]] = []

    fake_launchctl = tmp_path / "fake-launchctl"
    fake_launchctl.write_text("#!/bin/sh\nexit 0\n")
    fake_launchctl.chmod(0o755)

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    install(
        project_root=tmp_path,
        plist_path=plist_path,
        launchctl=str(fake_launchctl),
    )
    # The first call must be an unload of the prior plist; the
    # second a load of the freshly-written one.
    assert calls[0] == [str(fake_launchctl), "unload", str(plist_path)]
    assert calls[1] == [str(fake_launchctl), "load", str(plist_path)]


def test_install_raises_when_launchctl_load_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _force_macos(monkeypatch)
    plist_path = tmp_path / "com.rtl-buddy.hub.plist"
    fake_launchctl = tmp_path / "fake-launchctl"
    fake_launchctl.write_text("#!/bin/sh\nexit 1\n")
    fake_launchctl.chmod(0o755)

    def fake_run(cmd, **kwargs):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "boom: agent file is malformed"

        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(LaunchAgentError, match="launchctl load"):
        install(
            project_root=tmp_path,
            plist_path=plist_path,
            launchctl=str(fake_launchctl),
        )


def test_uninstall_removes_plist_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _force_macos(monkeypatch)
    plist_path = tmp_path / "com.rtl-buddy.hub.plist"
    plist_path.write_text("<?xml ?><plist></plist>")
    fake_launchctl = tmp_path / "fake-launchctl"
    fake_launchctl.write_text("#!/bin/sh\nexit 0\n")
    fake_launchctl.chmod(0o755)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    removed = uninstall(plist_path=plist_path, launchctl=str(fake_launchctl))
    assert removed is True
    assert not plist_path.exists()


def test_uninstall_returns_false_when_no_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _force_macos(monkeypatch)
    plist_path = tmp_path / "missing.plist"
    removed = uninstall(plist_path=plist_path)
    assert removed is False
