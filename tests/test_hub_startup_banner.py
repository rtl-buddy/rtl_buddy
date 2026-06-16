"""Tests for ``rtl_buddy.hub.loop._print_startup_banner``.

The banner is the first thing a user sees after ``rb hub start``;
silently blocking the terminal was the #1 papercut in early demos.
Exercise the conditional pieces:

* viewer URL only when ``--serve-viewer`` provided an http_port
* ``?view=/view.json`` suffix only when view_json_path resolves to
  an existing file (don't dangle a 404 URL)
* logs line only when ``[hub].log_path`` is configured
* TCP + the Ctrl-C hint are always present
"""

from __future__ import annotations

from pathlib import Path

from rtl_buddy.hub.loop import _print_startup_banner


def test_banner_tcp_only_no_viewer(capfd):
    """Without --serve-viewer there's no HTTP port — only TCP."""

    _print_startup_banner(
        tcp_host="127.0.0.1",
        tcp_port=12345,
        http_port=None,
        view_json_path=None,
        log_path=None,
    )
    out = capfd.readouterr().err
    assert "rtl-buddy-hub running." in out
    assert "TCP:      127.0.0.1:12345" in out
    assert "Viewer:" not in out
    assert "Logs:" not in out
    assert "Press Ctrl-C to stop." in out


def test_banner_with_viewer_but_no_view_json(capfd):
    """With --serve-viewer but no view.json on disk, the viewer URL
    omits the ?view=/view.json suffix so the user doesn't click into
    a 404."""

    _print_startup_banner(
        tcp_host="127.0.0.1",
        tcp_port=12345,
        http_port=54321,
        view_json_path=None,
        log_path=None,
    )
    out = capfd.readouterr().err
    assert "Viewer:   http://127.0.0.1:54321/" in out
    assert "?view=" not in out


def test_banner_view_json_path_set_but_file_missing(capfd, tmp_path: Path):
    """view_json_path configured but file doesn't exist → no ?view=
    suffix (matches the /view.json 404 behaviour from #141)."""

    missing = tmp_path / "view.json"
    _print_startup_banner(
        tcp_host="127.0.0.1",
        tcp_port=12345,
        http_port=54321,
        view_json_path=missing,
        log_path=None,
    )
    out = capfd.readouterr().err
    assert "Viewer:   http://127.0.0.1:54321/" in out
    assert "?view=" not in out


def test_banner_view_json_present_appends_query(capfd, tmp_path: Path):
    """view.json exists → URL includes the auto-load query string."""

    view_json = tmp_path / "view.json"
    view_json.write_text('{"schema_version":"1.0"}')
    _print_startup_banner(
        tcp_host="127.0.0.1",
        tcp_port=12345,
        http_port=54321,
        view_json_path=view_json,
        log_path=None,
    )
    out = capfd.readouterr().err
    assert "Viewer:   http://127.0.0.1:54321/?view=/view.json" in out


def test_banner_includes_log_path_when_configured(capfd, tmp_path: Path):
    log_path = tmp_path / "hub.log"
    _print_startup_banner(
        tcp_host="127.0.0.1",
        tcp_port=12345,
        http_port=None,
        view_json_path=None,
        log_path=log_path,
    )
    # Rich wraps long paths mid-string when the terminal width is narrow
    # (as it is under pytest capture). Strip whitespace before asserting so
    # the path-on-disk shows up as one contiguous substring.
    out = "".join(capfd.readouterr().err.split())
    assert "Logs:" in out
    assert "".join(str(log_path).split()) in out
