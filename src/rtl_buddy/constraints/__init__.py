"""Constraint-file (SDC / XDC) reading.

:mod:`~rtl_buddy.constraints.tcl_reader` returns :class:`~rtl_buddy.constraints.tcl_reader.TclCommand` records, using either a real Tcl interp (:mod:`~rtl_buddy.constraints.tcl_worker`) or the vendored word tokenizer (:mod:`~rtl_buddy.constraints.tcl_tokenizer`).
"""

from __future__ import annotations

from .tcl_reader import TclCommand, read_commands

__all__ = ["TclCommand", "read_commands"]
