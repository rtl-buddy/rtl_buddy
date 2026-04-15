# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""
verible module handles interfacing with the verible tool for rtl-buddy
"""
import logging
logger = logging.getLogger(__name__)
import pprint
import subprocess
import sys

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

class Verible:

  def __init__(self, name, cfg):
    self.name = name
    self.cfg = cfg

    log_event(logger, logging.DEBUG, "verible.config", config=pprint.pformat(cfg))

  def get_exe_path(self, exe_name):
    return self.cfg.get_exe_path(exe_name)

  def do_exe(self, exe_name, verible_args):
    """
    run verible executable
    """
    cmd = [self.get_exe_path(exe_name)]
    cmd += verible_args
    log_event(logger, logging.INFO, "verible.command", executable=exe_name, argv=" ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
      sys.stdout.write(result.stdout)
      sys.stdout.flush()
    if result.stderr:
      sys.stderr.write(result.stderr)
      sys.stderr.flush()
    log_event(logger, logging.INFO, "verible.completed", executable=exe_name, returncode=result.returncode)
    return result.returncode

  def do_lint(self, verible_args):
    args = self.cfg.get_extra_args('lint')
    args += verible_args
    return self.do_exe("verible-verilog-lint", args)

  def do_obfuscate(self, verible_args):
    assert False, "not supported yet"
    # obfuscate needs to use input output pipe, need to use different do_exe()
    return self.do_exe("verible-verilog-obfuscate", verible_args)

  def do_preprocessor(self, verible_args):
    return self.do_exe("verible-verilog-preprocessor", verible_args)

  def do_syntax(self, verible_args):
    return self.do_exe("verible-verilog-syntax", verible_args)

  def do_format(self, verible_args):
    return self.do_exe("verible-verilog-format", verible_args)

  def do_cmd(self, cmd, verible_args):
    #logger.info(cmd)
    if cmd == "lint":
      return self.do_lint(verible_args)
    elif cmd == "obfuscate":
      return self.do_obfuscate(verible_args)
    elif cmd == "preprocessor":
      return self.do_preprocessor(verible_args)
    elif cmd == "syntax":
      return self.do_syntax(verible_args)
    elif cmd == "format":
      return self.do_format(verible_args)
    else:
      log_event(logger, logging.ERROR, "verible.command_invalid", command=cmd)
      raise FatalRtlBuddyError(f"invalid command '{cmd}'")
