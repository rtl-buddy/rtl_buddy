"""
Tests for Surfer WCP integration: config path resolution, editor command
formatting, WCP frame I/O, and source resolver signal extraction.
"""

import logging
import os
import socket
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from rtl_buddy.config.surfer import SurferConfig, SurferConfigFile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_surfer_cfg(
    *,
    path="surfer",
    wcp_port=0,
    editor_cmd="vim +%l %f",
    editor_terminal="tmux",
    editor_sock="",
    ctrl_sock="",
    root_cfg_path="/proj/root_config.yaml",
    available=True,
):
    return SurferConfig(
        name="surfer-default",
        path=path,
        wcp_port=wcp_port,
        editor_cmd=editor_cmd,
        editor_terminal=editor_terminal,
        editor_sock=editor_sock,
        ctrl_sock=ctrl_sock,
        root_cfg_path=root_cfg_path,
        available=available,
    )


# ---------------------------------------------------------------------------
# SurferConfig: path resolution
# ---------------------------------------------------------------------------


class TestSurferConfigPathResolution:
    def test_bare_name_uses_which(self, tmp_path):
        fake_exe = tmp_path / "surfer"
        fake_exe.touch(mode=0o755)
        cfg = _make_surfer_cfg(
            path="surfer", root_cfg_path=str(tmp_path / "root_config.yaml")
        )
        with patch("shutil.which", return_value=str(fake_exe)):
            assert cfg.get_surfer_exe() == str(fake_exe)

    def test_bare_name_falls_back_to_name_when_not_found(self):
        cfg = _make_surfer_cfg(path="surfer")
        with patch("shutil.which", return_value=None):
            assert cfg.get_surfer_exe() == "surfer"

    def test_relative_path_resolved_from_root_config(self, tmp_path):
        root_cfg = tmp_path / "root_config.yaml"
        cfg = _make_surfer_cfg(
            path="../surfer/target/release/surfer",
            root_cfg_path=str(root_cfg),
        )
        expected = os.path.join(str(tmp_path), "../surfer/target/release/surfer")
        assert cfg.get_surfer_exe() == expected

    def test_dotslash_path_treated_as_relative(self, tmp_path):
        root_cfg = tmp_path / "root_config.yaml"
        cfg = _make_surfer_cfg(path="./bin/surfer", root_cfg_path=str(root_cfg))
        assert (
            cfg.get_surfer_exe().endswith("./bin/surfer") or "/" in cfg.get_surfer_exe()
        )

    def test_absolute_path_returned_as_is(self, tmp_path):
        abs_path = "/usr/local/bin/surfer"
        cfg = _make_surfer_cfg(
            path=abs_path, root_cfg_path=str(tmp_path / "root_config.yaml")
        )
        assert cfg.get_surfer_exe() == abs_path


# ---------------------------------------------------------------------------
# SurferConfig: editor command formatting
# ---------------------------------------------------------------------------


class TestSurferConfigEditorCmd:
    def test_f_and_l_substituted(self):
        cfg = _make_surfer_cfg(editor_cmd="vim +%l %f")
        assert (
            cfg.format_editor_cmd("/path/to/file.sv", 42) == "vim +42 /path/to/file.sv"
        )

    def test_vscode_format(self):
        cfg = _make_surfer_cfg(editor_cmd="code --goto %f:%l")
        assert cfg.format_editor_cmd("/src/foo.sv", 10) == "code --goto /src/foo.sv:10"

    def test_nvim_format(self):
        cfg = _make_surfer_cfg(editor_cmd="nvim +%l %f")
        assert cfg.format_editor_cmd("/src/bar.sv", 1) == "nvim +1 /src/bar.sv"

    def test_custom_script_format(self):
        cfg = _make_surfer_cfg(editor_cmd="/path/to/open.sh %f %l")
        assert (
            cfg.format_editor_cmd("/src/baz.sv", 99)
            == "/path/to/open.sh /src/baz.sv 99"
        )

    def test_no_placeholders_returns_cmd_unchanged(self):
        cfg = _make_surfer_cfg(editor_cmd="code .")
        assert cfg.format_editor_cmd("/src/foo.sv", 5) == "code ."


# ---------------------------------------------------------------------------
# SurferConfigFile.initialise: available flag
# ---------------------------------------------------------------------------


class TestSurferConfigFileInitialise:
    def test_available_true_when_bare_name_on_path(self, tmp_path):
        root_cfg = str(tmp_path / "root_config.yaml")
        cf = SurferConfigFile(
            name="s",
            path="surfer",
            wcp_port=0,
            editor_cmd="vim +%l %f",
            editor_terminal="tmux",
        )
        with patch("shutil.which", return_value="/usr/bin/surfer"):
            cfg = cf.initialise(root_cfg)
        assert cfg.available is True

    def test_available_false_when_bare_name_not_on_path(self, tmp_path):
        root_cfg = str(tmp_path / "root_config.yaml")
        cf = SurferConfigFile(
            name="s",
            path="surfer",
            wcp_port=0,
            editor_cmd="vim +%l %f",
            editor_terminal="tmux",
        )
        with patch("shutil.which", return_value=None):
            cfg = cf.initialise(root_cfg)
        assert cfg.available is False

    def test_available_true_when_relative_path_exists(self, tmp_path):
        exe = tmp_path / "bin" / "surfer"
        exe.parent.mkdir()
        exe.touch(mode=0o755)
        root_cfg = str(tmp_path / "root_config.yaml")
        cf = SurferConfigFile(
            name="s",
            path="bin/surfer",
            wcp_port=0,
            editor_cmd="vim +%l %f",
            editor_terminal="tmux",
        )
        cfg = cf.initialise(root_cfg)
        assert cfg.available is True

    def test_available_false_when_relative_path_missing(self, tmp_path):
        root_cfg = str(tmp_path / "root_config.yaml")
        cf = SurferConfigFile(
            name="s",
            path="bin/surfer",
            wcp_port=0,
            editor_cmd="vim +%l %f",
            editor_terminal="tmux",
        )
        cfg = cf.initialise(root_cfg)
        assert cfg.available is False


# ---------------------------------------------------------------------------
# WCP frame I/O
# ---------------------------------------------------------------------------


class TestWcpFrameIO:
    """Test null-byte delimited JSON framing using a socketpair."""

    def _make_pair(self):
        a, b = socket.socketpair()
        return a, b

    def test_send_and_receive_single_frame(self):
        from rtl_buddy.tools.surfer_wcp import _FrameReader, _send_frame

        a, b = self._make_pair()
        try:
            _send_frame(
                a,
                {"type": "greeting", "version": "0", "commands": ["goto_declaration"]},
            )
            reader = _FrameReader(b)
            msg = reader.read()
            assert msg["type"] == "greeting"
            assert msg["version"] == "0"
            assert "goto_declaration" in msg["commands"]
        finally:
            a.close()
            b.close()

    def test_multiple_frames_in_sequence(self):
        from rtl_buddy.tools.surfer_wcp import _FrameReader, _send_frame

        a, b = self._make_pair()
        try:
            _send_frame(a, {"type": "greeting", "version": "0", "commands": []})
            _send_frame(
                a,
                {
                    "type": "event",
                    "event": "goto_declaration",
                    "variable": "tb_top.clk",
                },
            )
            reader = _FrameReader(b)
            m1 = reader.read()
            m2 = reader.read()
            assert m1["type"] == "greeting"
            assert m2["event"] == "goto_declaration"
            assert m2["variable"] == "tb_top.clk"
        finally:
            a.close()
            b.close()

    def test_connection_error_on_closed_socket(self):
        from rtl_buddy.tools.surfer_wcp import _FrameReader

        a, b = self._make_pair()
        a.close()
        reader = _FrameReader(b)
        with pytest.raises(ConnectionError):
            reader.read()
        b.close()


# ---------------------------------------------------------------------------
# SurferSourceResolver: signal extraction logic
# ---------------------------------------------------------------------------


class TestSurferSourceResolver:
    """Test the resolver's variable→signal parsing and grep dispatch."""

    def _make_resolver_with_files(self, sv_files):
        """Build a resolver with a pre-set file list, bypassing VlogFilelist."""
        from rtl_buddy.tools.surfer_wcp import SurferSourceResolver

        resolver = object.__new__(SurferSourceResolver)
        resolver._sv_files = sv_files
        return resolver

    def test_resolve_finds_signal_in_sv_file(self, tmp_path):
        sv = tmp_path / "tb_top.sv"
        sv.write_text("module tb_top;\n  logic clk;\n  logic rst;\nendmodule\n")
        resolver = self._make_resolver_with_files([str(sv)])
        result = resolver.resolve("tb_top.clk")
        assert result is not None
        filepath, lineno = result
        assert filepath == str(sv)
        assert lineno == 2

    def test_resolve_returns_none_when_not_found(self, tmp_path):
        sv = tmp_path / "empty.sv"
        sv.write_text("module foo;\nendmodule\n")
        resolver = self._make_resolver_with_files([str(sv)])
        result = resolver.resolve("tb_top.nonexistent_signal_xyz")
        assert result is None

    def test_resolve_strips_trailing_digits_from_instance_fallback(self, tmp_path):
        sv = tmp_path / "design.sv"
        sv.write_text("module test_module_3;\n  logic z_bus;\nendmodule\n")
        resolver = self._make_resolver_with_files([str(sv)])
        # Signal "z_bus" found directly; no need for fallback
        result = resolver.resolve("tb_top.i_dut_2.z_bus")
        assert result is not None
        assert result[1] == 2

    def test_resolve_uses_module_fallback_when_signal_not_found(self, tmp_path):
        sv = tmp_path / "design.sv"
        # Only the module name exists, not a signal named "i_z"
        sv.write_text("module test_module_2;\n  // i_m2 instance\nendmodule\n")
        resolver = self._make_resolver_with_files([str(sv)])
        # "i_z" not found; fallback to "gen_i" → strip digits → "gen_i" → not found either
        # then "i_m2" → found on line 2
        result = resolver.resolve("tb_top.i_dut_2.gen_i.i_m2")
        assert result is not None

    def test_resolve_empty_file_list_returns_none(self):
        resolver = self._make_resolver_with_files([])
        assert resolver.resolve("tb_top.clk") is None

    def test_resolve_single_component_variable(self, tmp_path):
        sv = tmp_path / "top.sv"
        sv.write_text("logic clk;\n")
        resolver = self._make_resolver_with_files([str(sv)])
        result = resolver.resolve("clk")
        assert result is not None
        assert result[1] == 1


# ---------------------------------------------------------------------------
# WaveformValueReader
# ---------------------------------------------------------------------------


class TestWaveformValueReader:
    """Test value lookup via pywellen (mocked)."""

    def _make_reader(self, fst_path: str = "/fake/dump.fst"):
        from rtl_buddy.tools.surfer_wcp import WaveformValueReader

        return WaveformValueReader(fst_path)

    def test_get_value_returns_string_from_pywellen(self):
        reader = self._make_reader()
        mock_sig = SimpleNamespace(value_at_time=lambda t: "1'b1")
        mock_wf = SimpleNamespace(get_signal_from_path=lambda path: mock_sig)
        mock_pywellen = SimpleNamespace(Waveform=lambda path: mock_wf)
        with patch.dict("sys.modules", {"pywellen": mock_pywellen}):
            reader._waveform = mock_wf
            result = reader.get_value("tb_top.clk", 1000)
        assert result == "1'b1"

    def test_get_value_returns_none_on_signal_not_found(self):
        def bad_path(path):
            # pywellen reports a lookup miss as RuntimeError
            raise RuntimeError(f"No var at path {path}")

        reader = self._make_reader()
        reader._waveform = SimpleNamespace(get_signal_from_path=bad_path)
        result = reader.get_value("tb_top.nonexistent", 1000)
        assert result is None

    def test_get_value_raises_fatal_on_missing_trace(self):
        from rtl_buddy.errors import FatalRtlBuddyError
        from rtl_buddy.tools.surfer_wcp import WaveformValueReader

        reader = WaveformValueReader("/nonexistent/dump.fst")
        # A missing trace must fail loudly, not blank annotations (#263)
        with pytest.raises(FatalRtlBuddyError, match="not found"):
            reader.get_value("tb_top.clk", 1000)

    def test_get_value_propagates_unexpected_lookup_error(self):
        def broken_api(path):
            raise AttributeError("'Waveform' object has no attribute ...")

        reader = self._make_reader()
        reader._waveform = SimpleNamespace(get_signal_from_path=broken_api)
        # Only the pywellen lookup miss (RuntimeError) is swallowed —
        # an API break must surface, not return None (#263)
        with pytest.raises(AttributeError):
            reader.get_value("tb_top.clk", 1000)


# ---------------------------------------------------------------------------
# SurferWcpListener._emit_value: value annotation console output
# ---------------------------------------------------------------------------


class TestWcpValueEmission:
    """Unit-test _emit_value directly — no network, no threads."""

    def _make_listener(self, value_reader=None):
        from rtl_buddy.tools.surfer_wcp import (
            SurferSourceResolver,
            EditorLauncher,
            SurferWcpListener,
        )

        surfer_cfg = _make_surfer_cfg()
        resolver = object.__new__(SurferSourceResolver)
        resolver._sv_files = []
        editor = object.__new__(EditorLauncher)
        editor._surfer_cfg = surfer_cfg
        return SurferWcpListener(surfer_cfg, resolver, editor, value_reader)

    def test_emits_value_when_reader_and_timestamp_present(self):
        from rtl_buddy.tools.surfer_wcp import WaveformValueReader

        reader = WaveformValueReader("/fake/dump.fst")
        reader._waveform = SimpleNamespace(
            get_signal_from_path=lambda path: SimpleNamespace(
                value_at_time=lambda t: "1'b1"
            )
        )
        listener = self._make_listener(reader)
        emitted = []
        with patch(
            "rtl_buddy.tools.surfer_wcp.emit_console_text", side_effect=emitted.append
        ):
            listener._emit_value("tb_top.clk", 500)
        assert len(emitted) == 1
        assert "tb_top.clk" in emitted[0]
        assert "1'b1" in emitted[0]
        assert "t=500" in emitted[0]

    def test_no_emit_when_timestamp_is_none(self):
        from rtl_buddy.tools.surfer_wcp import WaveformValueReader

        reader = WaveformValueReader("/fake/dump.fst")
        reader._waveform = SimpleNamespace(
            get_signal_from_path=lambda path: SimpleNamespace(
                value_at_time=lambda t: "1'b0"
            )
        )
        listener = self._make_listener(reader)
        emitted = []
        with patch(
            "rtl_buddy.tools.surfer_wcp.emit_console_text", side_effect=emitted.append
        ):
            listener._emit_value("tb_top.clk", None)
        assert emitted == []

    def test_no_emit_when_no_reader(self):
        listener = self._make_listener(value_reader=None)
        emitted = []
        with patch(
            "rtl_buddy.tools.surfer_wcp.emit_console_text", side_effect=emitted.append
        ):
            listener._emit_value("tb_top.clk", 500)
        assert emitted == []

    def test_no_emit_when_value_not_found(self):
        from rtl_buddy.tools.surfer_wcp import WaveformValueReader

        reader = WaveformValueReader("/fake/dump.fst")

        def bad_path(path):
            raise RuntimeError(f"No var at path {path}")

        reader._waveform = SimpleNamespace(get_signal_from_path=bad_path)
        listener = self._make_listener(reader)
        emitted = []
        with patch(
            "rtl_buddy.tools.surfer_wcp.emit_console_text", side_effect=emitted.append
        ):
            listener._emit_value("tb_top.missing", 100)
        assert emitted == []


# ---------------------------------------------------------------------------
# _instance_name helper
# ---------------------------------------------------------------------------


class TestInstanceName:
    def test_three_component_path_returns_middle(self):
        from rtl_buddy.tools.surfer_wcp import _instance_name

        assert _instance_name("tb_top.i_dut.clk") == "i_dut"

    def test_two_component_path_returns_first(self):
        from rtl_buddy.tools.surfer_wcp import _instance_name

        assert _instance_name("tb_top.clk") == "tb_top"

    def test_single_component_returns_itself(self):
        from rtl_buddy.tools.surfer_wcp import _instance_name

        assert _instance_name("clk") == "clk"

    def test_deep_path_returns_second_to_last(self):
        from rtl_buddy.tools.surfer_wcp import _instance_name

        assert _instance_name("tb_top.i_dut.i_sub.rst_n") == "i_sub"


# ---------------------------------------------------------------------------
# ScopeAnnotationCache
# ---------------------------------------------------------------------------


class TestScopeAnnotationCache:
    def test_single_signal_maps_to_correct_file_and_line(self, tmp_path):
        from rtl_buddy.tools.surfer_wcp import ScopeAnnotationCache

        sv = tmp_path / "design.sv"
        sv.write_text("module foo;\n  logic clk;\nendmodule\n")
        signals = [("clk", "tb_top.i_dut.clk")]
        cache = ScopeAnnotationCache("tb_top.i_dut", signals, [str(sv)])
        assert "tb_top.i_dut.clk" in cache.path_map
        filepath, lineno = cache.path_map["tb_top.i_dut.clk"]
        assert filepath == str(sv)
        assert lineno == 2

    def test_two_signals_on_same_line_both_in_path_map(self, tmp_path):
        from rtl_buddy.tools.surfer_wcp import ScopeAnnotationCache

        sv = tmp_path / "design.sv"
        sv.write_text("module foo;\n  logic a, b;\nendmodule\n")
        signals = [("a", "tb_top.i_dut.a"), ("b", "tb_top.i_dut.b")]
        cache = ScopeAnnotationCache("tb_top.i_dut", signals, [str(sv)])
        assert "tb_top.i_dut.a" in cache.path_map
        assert "tb_top.i_dut.b" in cache.path_map
        # Both must point to the same file and line
        assert cache.path_map["tb_top.i_dut.a"] == cache.path_map["tb_top.i_dut.b"]

    def test_signal_not_found_in_any_file_not_in_path_map(self, tmp_path):
        from rtl_buddy.tools.surfer_wcp import ScopeAnnotationCache

        sv = tmp_path / "design.sv"
        sv.write_text("module foo;\n  logic irrelevant;\nendmodule\n")
        signals = [("missing_signal_xyz", "tb_top.i_dut.missing_signal_xyz")]
        cache = ScopeAnnotationCache("tb_top.i_dut", signals, [str(sv)])
        assert "tb_top.i_dut.missing_signal_xyz" not in cache.path_map

    def test_empty_signals_list_gives_empty_path_map(self, tmp_path):
        from rtl_buddy.tools.surfer_wcp import ScopeAnnotationCache

        sv = tmp_path / "design.sv"
        sv.write_text("module foo;\n  logic clk;\nendmodule\n")
        cache = ScopeAnnotationCache("tb_top.i_dut", [], [str(sv)])
        assert cache.path_map == {}


# ---------------------------------------------------------------------------
# WaveformValueReader.get_values_bulk
# ---------------------------------------------------------------------------


class TestWaveformValueReaderBulk:
    def _make_reader(self, fst_path: str = "/fake/dump.fst"):
        from rtl_buddy.tools.surfer_wcp import WaveformValueReader

        return WaveformValueReader(fst_path)

    def test_returns_dict_with_found_values(self):
        reader = self._make_reader()
        mock_wf = SimpleNamespace(
            get_signal_from_path=lambda path: SimpleNamespace(
                value_at_time=lambda t: "1'b1"
            )
        )
        reader._waveform = mock_wf
        result = reader.get_values_bulk(["tb_top.i_dut.clk", "tb_top.i_dut.rst"], 500)
        assert result == {
            "tb_top.i_dut.clk": "1'b1",
            "tb_top.i_dut.rst": "1'b1",
        }

    def test_missing_signal_omitted_not_raised(self):
        def bad_path(path):
            if "missing" in path:
                raise RuntimeError(f"No var at path {path}")
            return SimpleNamespace(value_at_time=lambda t: "1'b0")

        reader = self._make_reader()
        reader._waveform = SimpleNamespace(get_signal_from_path=bad_path)
        result = reader.get_values_bulk(
            ["tb_top.i_dut.clk", "tb_top.i_dut.missing"], 100
        )
        assert "tb_top.i_dut.clk" in result
        assert "tb_top.i_dut.missing" not in result

    def test_missing_trace_raises_fatal(self):
        from rtl_buddy.errors import FatalRtlBuddyError
        from rtl_buddy.tools.surfer_wcp import WaveformValueReader

        reader = WaveformValueReader("/nonexistent/dump.fst")
        with pytest.raises(FatalRtlBuddyError, match="not found"):
            reader.get_values_bulk(["tb_top.i_dut.clk"], 100)


# ---------------------------------------------------------------------------
# WaveformValueReader.get_scope_signals
# ---------------------------------------------------------------------------


class TestWaveformValueReaderScopeSignals:
    @staticmethod
    def _make_var(name, full_name):
        return SimpleNamespace(
            name=lambda h, n=name: n, full_name=lambda h, f=full_name: f
        )

    @staticmethod
    def _make_scope(full_name, variables=(), children=()):
        return SimpleNamespace(
            full_name=lambda h, f=full_name: f,
            vars=lambda h, v=variables: list(v),
            scopes=lambda h, c=children: list(c),
        )

    def test_missing_trace_raises_fatal(self):
        from rtl_buddy.errors import FatalRtlBuddyError
        from rtl_buddy.tools.surfer_wcp import WaveformValueReader

        reader = WaveformValueReader("/nonexistent/dump.fst")
        with pytest.raises(FatalRtlBuddyError, match="not found"):
            reader.get_scope_signals("tb_top.i_dut")

    def test_returns_signals_under_matching_scope(self):
        from rtl_buddy.tools.surfer_wcp import WaveformValueReader

        clk = self._make_var("clk", "tb_top.i_dut.clk")
        rst = self._make_var("rst", "tb_top.i_dut.rst")
        i_dut = self._make_scope("tb_top.i_dut", variables=(clk, rst))
        tb_top = self._make_scope("tb_top", children=(i_dut,))
        h = SimpleNamespace(top_scopes=lambda: [tb_top])

        reader = WaveformValueReader("/fake/dump.fst")
        reader._waveform = SimpleNamespace(hierarchy=h)
        result = reader.get_scope_signals("tb_top.i_dut")
        assert result == [
            ("clk", "tb_top.i_dut.clk"),
            ("rst", "tb_top.i_dut.rst"),
        ]

    def test_no_match_returns_empty(self):
        from rtl_buddy.tools.surfer_wcp import WaveformValueReader

        tb_top = self._make_scope("tb_top")
        h = SimpleNamespace(top_scopes=lambda: [tb_top])
        reader = WaveformValueReader("/fake/dump.fst")
        reader._waveform = SimpleNamespace(hierarchy=h)
        assert reader.get_scope_signals("tb_top.i_other") == []


# ---------------------------------------------------------------------------
# WaveformValueReader.check — fail-loud preflight (#263)
# ---------------------------------------------------------------------------


class TestWaveformValueReaderCheck:
    def test_missing_trace_raises_fatal(self):
        from rtl_buddy.errors import FatalRtlBuddyError
        from rtl_buddy.tools.surfer_wcp import WaveformValueReader

        reader = WaveformValueReader("/nonexistent/dump.fst")
        with pytest.raises(FatalRtlBuddyError, match="not found"):
            reader.check()

    def test_pywellen_without_random_access_api_raises_fatal(self, tmp_path):
        from rtl_buddy.errors import FatalRtlBuddyError
        from rtl_buddy.tools.surfer_wcp import WaveformValueReader

        fst = tmp_path / "dump.fst"
        fst.touch()
        # A streaming-only Waveform class (pywellen >=0.25 shape)
        fake_pywellen = SimpleNamespace(Waveform=type("Waveform", (), {}))
        reader = WaveformValueReader(str(fst))
        with patch.dict("sys.modules", {"pywellen": fake_pywellen}):
            with pytest.raises(FatalRtlBuddyError, match="random-access"):
                reader.check()

    def test_passes_with_random_access_api(self, tmp_path):
        from rtl_buddy.tools.surfer_wcp import WaveformValueReader

        fst = tmp_path / "dump.fst"
        fst.touch()
        attrs = {
            "hierarchy": property(lambda self: None),
            "get_signal": lambda self, v: None,
            "get_signal_from_path": lambda self, p: None,
        }
        fake_pywellen = SimpleNamespace(Waveform=type("Waveform", (), attrs))
        reader = WaveformValueReader(str(fst))
        with patch.dict("sys.modules", {"pywellen": fake_pywellen}):
            reader.check()  # must not raise


# ---------------------------------------------------------------------------
# _push_scope_values same-line grouping
# ---------------------------------------------------------------------------


class TestPushScopeValuesSameLineGrouping:
    """Unit-test the same-line grouping logic in _push_scope_values."""

    def _make_listener(self, scope_cache, value_reader):
        from rtl_buddy.tools.surfer_wcp import (
            SurferSourceResolver,
            EditorLauncher,
            SurferWcpListener,
        )

        surfer_cfg = _make_surfer_cfg(
            editor_sock="~/.local/share/nvim/surfer.sock", editor_cmd="nvim +%l %f"
        )
        resolver = object.__new__(SurferSourceResolver)
        resolver._sv_files = []
        editor = object.__new__(EditorLauncher)
        editor._surfer_cfg = surfer_cfg
        listener = SurferWcpListener(
            surfer_cfg, resolver, editor, value_reader, scope_annotation=True
        )
        listener._scope_cache = scope_cache
        return listener

    def test_two_signals_on_same_line_combined_into_one_annotation(self):
        from rtl_buddy.tools.surfer_wcp import ScopeAnnotationCache, EditorLauncher

        # Build a mock scope cache whose items() returns two signals at the same line
        fake_filepath = "/proj/foo.sv"
        fake_lineno = 5
        mock_cache = MagicMock(spec=ScopeAnnotationCache)
        mock_cache.scope_path = "tb_top.i_dut"
        mock_cache.items.return_value = [
            ("tb_top.i_dut.a", fake_filepath, fake_lineno),
            ("tb_top.i_dut.b", fake_filepath, fake_lineno),
        ]

        # Value reader returns both values
        mock_reader = MagicMock()
        mock_reader.get_values_bulk.return_value = {
            "tb_top.i_dut.a": "1'b0",
            "tb_top.i_dut.b": "1'b1",
        }

        listener = self._make_listener(mock_cache, mock_reader)

        captured_annotations = []

        def fake_nvim_socket_alive(path):
            return True

        def fake_nvim_remote_scope(sock_path, annotations):
            captured_annotations.extend(annotations)

        with patch.object(
            EditorLauncher, "_nvim_socket_alive", staticmethod(fake_nvim_socket_alive)
        ):
            with patch.object(
                EditorLauncher,
                "_nvim_remote_scope",
                staticmethod(fake_nvim_remote_scope),
            ):
                listener._push_scope_values(1000)

        # Both signals should be collapsed into a single annotation entry
        assert len(captured_annotations) == 1
        ann_lineno, ann_display, ann_filepath = captured_annotations[0]
        assert ann_lineno == fake_lineno
        assert ann_filepath == fake_filepath
        # Combined display must mention both signal names
        assert "a=" in ann_display
        assert "b=" in ann_display


# ---------------------------------------------------------------------------
# WaveLauncher._check_nvim_plugin
# ---------------------------------------------------------------------------


class TestWaveLauncherCheckNvimPlugin:
    def _make_launcher(self, surfer_cfg):
        from rtl_buddy.tools.wave_launcher import WaveLauncher

        launcher = object.__new__(WaveLauncher)
        launcher._surfer_cfg = surfer_cfg
        launcher._test_cfg = MagicMock()
        launcher._suite_dir = "/fake/suite"
        launcher._fst_path = "/fake/dump.fst"
        launcher._surfer_file = None
        launcher._scope_annotation = True
        return launcher

    def _warnings(self, launcher):
        """Run the check, capturing WARNING-level log_event calls."""
        log_calls = []
        with patch(
            "rtl_buddy.tools.wave_launcher.log_event",
            side_effect=lambda *a, **kw: log_calls.append((a, kw)),
        ):
            launcher._check_nvim_plugin()
        return [c for c in log_calls if c[0][1] == logging.WARNING]

    def test_warning_logged_when_plugin_missing(self):
        surfer_cfg = _make_surfer_cfg(
            editor_sock="~/.local/share/nvim/surfer.sock",
            editor_cmd="nvim +%l %f",
        )
        launcher = self._make_launcher(surfer_cfg)
        with patch("rtl_buddy.tools.nvim_install.is_installed", return_value=False):
            assert len(self._warnings(launcher)) >= 1

    def test_no_warning_when_plugin_installed(self):
        surfer_cfg = _make_surfer_cfg(
            editor_sock="~/.local/share/nvim/surfer.sock",
            editor_cmd="nvim +%l %f",
        )
        launcher = self._make_launcher(surfer_cfg)
        with patch("rtl_buddy.tools.nvim_install.is_installed", return_value=True):
            assert len(self._warnings(launcher)) == 0

    def test_no_warning_when_editor_sock_empty(self):
        surfer_cfg = _make_surfer_cfg(
            editor_sock="",  # no sock configured — check returns before is_installed
            editor_cmd="nvim +%l %f",
        )
        launcher = self._make_launcher(surfer_cfg)
        with patch("rtl_buddy.tools.nvim_install.is_installed", return_value=False):
            assert len(self._warnings(launcher)) == 0


# ---------------------------------------------------------------------------
# EditorLauncher._nvim_exec_lua
# ---------------------------------------------------------------------------


class TestEditorLauncherNvimExecLua:
    def test_uses_remote_expr_not_remote_send(self):
        from rtl_buddy.tools.surfer_wcp import EditorLauncher

        lua = "print('hello')"
        with patch("subprocess.Popen") as mock_popen:
            EditorLauncher._nvim_exec_lua("/tmp/nvim.sock", lua)
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert "--remote-expr" in cmd
        assert "--remote-send" not in cmd

    def test_command_contains_nvim_exec2(self):
        from rtl_buddy.tools.surfer_wcp import EditorLauncher

        lua = "print('hello')"
        with patch("subprocess.Popen") as mock_popen:
            EditorLauncher._nvim_exec_lua("/tmp/nvim.sock", lua)
        cmd = mock_popen.call_args[0][0]
        # The --remote-expr argument should reference nvim_exec2
        expr_idx = cmd.index("--remote-expr")
        expr_val = cmd[expr_idx + 1]
        assert "nvim_exec2" in expr_val

    def test_double_quotes_in_lua_are_escaped(self):
        from rtl_buddy.tools.surfer_wcp import EditorLauncher

        lua = 'local x = "hello"'
        with patch("subprocess.Popen") as mock_popen:
            EditorLauncher._nvim_exec_lua("/tmp/nvim.sock", lua)
        cmd = mock_popen.call_args[0][0]
        expr_idx = cmd.index("--remote-expr")
        expr_val = cmd[expr_idx + 1]
        # The double-quote must be escaped in the Vimscript string context
        assert '\\"' in expr_val


# ---------------------------------------------------------------------------
# pywellen API surface guard
# ---------------------------------------------------------------------------


class TestPywellenApiSurface:
    """Pin the pywellen API that rtl_buddy's trace readers depend on.

    pywellen 0.25.0 rewrote ``Waveform`` to a streaming-only surface,
    removing the random-access API below — which silently blanked
    ``rb wave`` value annotations and crashed ``rb saif`` (#263). The
    dependency is bounded to ``<0.25`` in pyproject; this test makes the
    next such rewrite fail loudly in CI at lock-bump time instead of in
    the field. Lift/adjust together with the bound when the readers are
    ported.
    """

    def test_waveform_random_access_api_present(self):
        import pywellen

        from rtl_buddy.tools.pywellen_compat import RANDOM_ACCESS_API

        # tools/surfer_wcp.WaveformValueReader + tools/saif_from_trace
        for attr in RANDOM_ACCESS_API:
            assert hasattr(pywellen.Waveform, attr), (
                f"pywellen.Waveform.{attr} missing — incompatible pywellen "
                "(>=0.25 streaming rewrite?); rb wave annotations and "
                "rb saif depend on the random-access API (#263)"
            )


# ---------------------------------------------------------------------------
# WaveLauncher: fail-loud preflight before Surfer starts (#263)
# ---------------------------------------------------------------------------


class TestWaveLauncherValueReaderPreflight:
    def test_launch_raises_on_missing_trace_before_surfer_starts(self):
        from rtl_buddy.errors import FatalRtlBuddyError
        from rtl_buddy.tools.wave_launcher import WaveLauncher

        launcher = object.__new__(WaveLauncher)
        launcher._surfer_cfg = _make_surfer_cfg()
        launcher._test_cfg = MagicMock()
        launcher._suite_dir = "/fake/suite"
        launcher._fst_path = "/nonexistent/dump.fst"
        launcher._surfer_file = None
        launcher._scope_annotation = True

        with (
            patch("rtl_buddy.tools.wave_launcher.SurferSourceResolver"),
            patch("rtl_buddy.tools.wave_launcher.EditorLauncher"),
            patch("rtl_buddy.tools.wave_launcher.subprocess.Popen") as popen,
        ):
            with pytest.raises(FatalRtlBuddyError, match="not found"):
                launcher.launch()
        popen.assert_not_called()


# ---------------------------------------------------------------------------
# SurferWcpListener.run: graceful teardown on FatalRtlBuddyError (#263)
# ---------------------------------------------------------------------------


class TestListenerFatalErrorTeardown:
    def test_fatal_from_handler_stops_listener_without_traceback(self):
        from rtl_buddy.errors import FatalRtlBuddyError
        from rtl_buddy.tools.surfer_wcp import (
            EditorLauncher,
            SurferSourceResolver,
            SurferWcpListener,
        )

        surfer_cfg = _make_surfer_cfg()
        resolver = object.__new__(SurferSourceResolver)
        resolver._sv_files = []
        editor = object.__new__(EditorLauncher)
        editor._surfer_cfg = surfer_cfg
        listener = SurferWcpListener(surfer_cfg, resolver, editor)

        fake_conn = MagicMock()
        srv = MagicMock()
        srv.accept.return_value = (fake_conn, ("127.0.0.1", 12345))
        listener._srv = srv

        with patch.object(
            listener,
            "_handle_connection",
            side_effect=FatalRtlBuddyError("could not open waveform trace"),
        ):
            listener.run()  # must return cleanly, not raise

        assert listener._stop.is_set()
        fake_conn.close.assert_called()


class TestWcpReplyWaiters:
    """Correlation of WCP response/error frames to pending reply waiters.

    These exercise the real SurferWcpListener waiter subsystem (the
    wave_hub_bridge tests use a fake listener), covering the genuine
    success/error reporting path: a response resolves by command name, an
    error resolves the first error-accepting waiter, and the back-compat
    await_response ignores errors.
    """

    def _make_listener(self):
        from rtl_buddy.tools.surfer_wcp import (
            SurferSourceResolver,
            EditorLauncher,
            SurferWcpListener,
        )

        surfer_cfg = _make_surfer_cfg()
        resolver = object.__new__(SurferSourceResolver)
        resolver._sv_files = []
        editor = object.__new__(EditorLauncher)
        editor._surfer_cfg = surfer_cfg
        return SurferWcpListener(surfer_cfg, resolver, editor)

    def _await_in_thread(self, fn):
        import threading

        box = {}

        def run():
            box["result"] = fn()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t, box

    def test_await_reply_resolves_on_matching_response(self):
        import time as _time

        listener = self._make_listener()
        frame = {"type": "response", "command": "ack"}
        t, box = self._await_in_thread(
            lambda: listener.await_reply({"ack"}, timeout=2.0)
        )
        # waiter is registered; dispatch the response
        deadline = _time.monotonic() + 1.0
        while _time.monotonic() < deadline and not listener._waiters:
            _time.sleep(0.01)
        listener._dispatch_response(frame)
        t.join(2.0)
        assert box["result"] == ("response", frame)

    def test_await_reply_resolves_on_error(self):
        import time as _time

        listener = self._make_listener()
        err = {"type": "error", "error": "move_items", "message": "bad id"}
        t, box = self._await_in_thread(
            lambda: listener.await_reply({"ack"}, timeout=2.0)
        )
        deadline = _time.monotonic() + 1.0
        while _time.monotonic() < deadline and not listener._waiters:
            _time.sleep(0.01)
        listener._dispatch_error(err)
        t.join(2.0)
        assert box["result"] == ("error", err)

    def test_dispatch_response_correlates_by_command(self):
        import time as _time

        listener = self._make_listener()
        # Two waiters: one for get_item_list, one for ack. A get_item_list
        # response must wake only the matching waiter.
        t1, box1 = self._await_in_thread(
            lambda: listener.await_reply({"get_item_list"}, timeout=2.0)
        )
        t2, box2 = self._await_in_thread(
            lambda: listener.await_reply({"ack"}, timeout=2.0)
        )
        deadline = _time.monotonic() + 1.0
        while _time.monotonic() < deadline and len(listener._waiters) < 2:
            _time.sleep(0.01)
        list_frame = {"type": "response", "command": "get_item_list", "ids": [1]}
        listener._dispatch_response(list_frame)
        t1.join(2.0)
        assert box1["result"] == ("response", list_frame)
        # The ack waiter is still pending.
        assert len(listener._waiters) == 1
        ack_frame = {"type": "response", "command": "ack"}
        listener._dispatch_response(ack_frame)
        t2.join(2.0)
        assert box2["result"] == ("response", ack_frame)

    def test_await_response_ignores_errors(self):
        """Back-compat await_response (accept_error=False) must not be
        resolved by an error frame — it times out instead, so the
        cursor-driven query path is unchanged."""
        listener = self._make_listener()
        t, box = self._await_in_thread(
            lambda: listener.await_response("query_variable_values", timeout=0.3)
        )
        # Dispatch an error; the response waiter should NOT pick it up.
        listener._dispatch_error({"type": "error", "error": "x", "message": "y"})
        t.join(2.0)
        assert box["result"] is None
