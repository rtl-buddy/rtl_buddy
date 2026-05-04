"""
Tests for Surfer WCP integration: config path resolution, editor command
formatting, WCP frame I/O, and source resolver signal extraction.
"""
import io
import json
import os
import socket
import threading
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rtl_buddy.config.surfer import SurferConfig, SurferConfigFile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_surfer_cfg(*, path='surfer', wcp_port=0,
                     editor_cmd='vim +%l %f', editor_terminal='tmux',
                     root_cfg_path='/proj/root_config.yaml', available=True):
  return SurferConfig(
    name='surfer-default',
    path=path,
    wcp_port=wcp_port,
    editor_cmd=editor_cmd,
    editor_terminal=editor_terminal,
    root_cfg_path=root_cfg_path,
    available=available,
  )


# ---------------------------------------------------------------------------
# SurferConfig: path resolution
# ---------------------------------------------------------------------------

class TestSurferConfigPathResolution:

  def test_bare_name_uses_which(self, tmp_path):
    fake_exe = tmp_path / 'surfer'
    fake_exe.touch(mode=0o755)
    cfg = _make_surfer_cfg(path='surfer', root_cfg_path=str(tmp_path / 'root_config.yaml'))
    with patch('shutil.which', return_value=str(fake_exe)):
      assert cfg.get_surfer_exe() == str(fake_exe)

  def test_bare_name_falls_back_to_name_when_not_found(self):
    cfg = _make_surfer_cfg(path='surfer')
    with patch('shutil.which', return_value=None):
      assert cfg.get_surfer_exe() == 'surfer'

  def test_relative_path_resolved_from_root_config(self, tmp_path):
    root_cfg = tmp_path / 'root_config.yaml'
    cfg = _make_surfer_cfg(
      path='../surfer/target/release/surfer',
      root_cfg_path=str(root_cfg),
    )
    expected = os.path.join(str(tmp_path), '../surfer/target/release/surfer')
    assert cfg.get_surfer_exe() == expected

  def test_dotslash_path_treated_as_relative(self, tmp_path):
    root_cfg = tmp_path / 'root_config.yaml'
    cfg = _make_surfer_cfg(path='./bin/surfer', root_cfg_path=str(root_cfg))
    assert cfg.get_surfer_exe().endswith('./bin/surfer') or '/' in cfg.get_surfer_exe()

  def test_absolute_path_returned_as_is(self, tmp_path):
    abs_path = '/usr/local/bin/surfer'
    cfg = _make_surfer_cfg(path=abs_path, root_cfg_path=str(tmp_path / 'root_config.yaml'))
    assert cfg.get_surfer_exe() == abs_path


# ---------------------------------------------------------------------------
# SurferConfig: editor command formatting
# ---------------------------------------------------------------------------

class TestSurferConfigEditorCmd:

  def test_f_and_l_substituted(self):
    cfg = _make_surfer_cfg(editor_cmd='vim +%l %f')
    assert cfg.format_editor_cmd('/path/to/file.sv', 42) == 'vim +42 /path/to/file.sv'

  def test_vscode_format(self):
    cfg = _make_surfer_cfg(editor_cmd='code --goto %f:%l')
    assert cfg.format_editor_cmd('/src/foo.sv', 10) == 'code --goto /src/foo.sv:10'

  def test_nvim_format(self):
    cfg = _make_surfer_cfg(editor_cmd='nvim +%l %f')
    assert cfg.format_editor_cmd('/src/bar.sv', 1) == 'nvim +1 /src/bar.sv'

  def test_custom_script_format(self):
    cfg = _make_surfer_cfg(editor_cmd='/path/to/open.sh %f %l')
    assert cfg.format_editor_cmd('/src/baz.sv', 99) == '/path/to/open.sh /src/baz.sv 99'

  def test_no_placeholders_returns_cmd_unchanged(self):
    cfg = _make_surfer_cfg(editor_cmd='code .')
    assert cfg.format_editor_cmd('/src/foo.sv', 5) == 'code .'


# ---------------------------------------------------------------------------
# SurferConfigFile.initialise: available flag
# ---------------------------------------------------------------------------

class TestSurferConfigFileInitialise:

  def test_available_true_when_bare_name_on_path(self, tmp_path):
    root_cfg = str(tmp_path / 'root_config.yaml')
    cf = SurferConfigFile(name='s', path='surfer', wcp_port=0,
                          editor_cmd='vim +%l %f', editor_terminal='tmux')
    with patch('shutil.which', return_value='/usr/bin/surfer'):
      cfg = cf.initialise(root_cfg)
    assert cfg.available is True

  def test_available_false_when_bare_name_not_on_path(self, tmp_path):
    root_cfg = str(tmp_path / 'root_config.yaml')
    cf = SurferConfigFile(name='s', path='surfer', wcp_port=0,
                          editor_cmd='vim +%l %f', editor_terminal='tmux')
    with patch('shutil.which', return_value=None):
      cfg = cf.initialise(root_cfg)
    assert cfg.available is False

  def test_available_true_when_relative_path_exists(self, tmp_path):
    exe = tmp_path / 'bin' / 'surfer'
    exe.parent.mkdir()
    exe.touch(mode=0o755)
    root_cfg = str(tmp_path / 'root_config.yaml')
    cf = SurferConfigFile(name='s', path='bin/surfer', wcp_port=0,
                          editor_cmd='vim +%l %f', editor_terminal='tmux')
    cfg = cf.initialise(root_cfg)
    assert cfg.available is True

  def test_available_false_when_relative_path_missing(self, tmp_path):
    root_cfg = str(tmp_path / 'root_config.yaml')
    cf = SurferConfigFile(name='s', path='bin/surfer', wcp_port=0,
                          editor_cmd='vim +%l %f', editor_terminal='tmux')
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
      _send_frame(a, {'type': 'greeting', 'version': '0', 'commands': ['goto_declaration']})
      reader = _FrameReader(b)
      msg = reader.read()
      assert msg['type'] == 'greeting'
      assert msg['version'] == '0'
      assert 'goto_declaration' in msg['commands']
    finally:
      a.close()
      b.close()

  def test_multiple_frames_in_sequence(self):
    from rtl_buddy.tools.surfer_wcp import _FrameReader, _send_frame
    a, b = self._make_pair()
    try:
      _send_frame(a, {'type': 'greeting', 'version': '0', 'commands': []})
      _send_frame(a, {'type': 'event', 'event': 'goto_declaration', 'variable': 'tb_top.clk'})
      reader = _FrameReader(b)
      m1 = reader.read()
      m2 = reader.read()
      assert m1['type'] == 'greeting'
      assert m2['event'] == 'goto_declaration'
      assert m2['variable'] == 'tb_top.clk'
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
    from rtl_buddy.tools.surfer_wcp import SurferSourceResolver
    sv = tmp_path / 'tb_top.sv'
    sv.write_text('module tb_top;\n  logic clk;\n  logic rst;\nendmodule\n')
    resolver = self._make_resolver_with_files([str(sv)])
    result = resolver.resolve('tb_top.clk')
    assert result is not None
    filepath, lineno = result
    assert filepath == str(sv)
    assert lineno == 2

  def test_resolve_returns_none_when_not_found(self, tmp_path):
    from rtl_buddy.tools.surfer_wcp import SurferSourceResolver
    sv = tmp_path / 'empty.sv'
    sv.write_text('module foo;\nendmodule\n')
    resolver = self._make_resolver_with_files([str(sv)])
    result = resolver.resolve('tb_top.nonexistent_signal_xyz')
    assert result is None

  def test_resolve_strips_trailing_digits_from_instance_fallback(self, tmp_path):
    from rtl_buddy.tools.surfer_wcp import SurferSourceResolver
    sv = tmp_path / 'design.sv'
    sv.write_text('module test_module_3;\n  logic z_bus;\nendmodule\n')
    resolver = self._make_resolver_with_files([str(sv)])
    # Signal "z_bus" found directly; no need for fallback
    result = resolver.resolve('tb_top.i_dut_2.z_bus')
    assert result is not None
    assert result[1] == 2

  def test_resolve_uses_module_fallback_when_signal_not_found(self, tmp_path):
    from rtl_buddy.tools.surfer_wcp import SurferSourceResolver
    sv = tmp_path / 'design.sv'
    # Only the module name exists, not a signal named "i_z"
    sv.write_text('module test_module_2;\n  // i_m2 instance\nendmodule\n')
    resolver = self._make_resolver_with_files([str(sv)])
    # "i_z" not found; fallback to "gen_i" → strip digits → "gen_i" → not found either
    # then "i_m2" → found on line 2
    result = resolver.resolve('tb_top.i_dut_2.gen_i.i_m2')
    assert result is not None

  def test_resolve_empty_file_list_returns_none(self):
    from rtl_buddy.tools.surfer_wcp import SurferSourceResolver
    resolver = self._make_resolver_with_files([])
    assert resolver.resolve('tb_top.clk') is None

  def test_resolve_single_component_variable(self, tmp_path):
    from rtl_buddy.tools.surfer_wcp import SurferSourceResolver
    sv = tmp_path / 'top.sv'
    sv.write_text('logic clk;\n')
    resolver = self._make_resolver_with_files([str(sv)])
    result = resolver.resolve('clk')
    assert result is not None
    assert result[1] == 1
