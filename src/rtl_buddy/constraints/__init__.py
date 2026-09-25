"""Constraint-file (SDC / XDC) reading.

:mod:`~rtl_buddy.constraints.tcl_tokenizer` is a vendored copy of
rtl-buddy-cdc's Tcl-aware word tokenizer;
:mod:`~rtl_buddy.constraints.tcl_reader` wraps it in the
:class:`~rtl_buddy.constraints.tcl_reader.TclCommand` shape every
constraint consumer in this repo reads through, so the backend can be
swapped (rtl-buddy/rtl_buddy#641 adds a real Tcl interp) without
touching the consumers.
"""

from __future__ import annotations

from .tcl_reader import TclCommand, read_commands

__all__ = ["TclCommand", "read_commands"]
