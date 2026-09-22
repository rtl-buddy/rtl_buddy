"""Contract tests for the Tcl constraint reader (rtl-buddy/rtl_buddy#642).

Every row of the issue's reproduction tables is pinned here: the shapes the
old per-line regexes mis-read (continuation, braced values, nested
collections, ``#`` inside braces) plus the one-warning-per-file contract for
Tcl the tokenizer does not evaluate.
"""

from __future__ import annotations

import logging

import pytest

from rtl_buddy.constraints.tcl_reader import (
    TclCommand,
    backend_name,
    extract_names,
    read_commands,
)

CLOCK = frozenset({"create_clock"})


def _events(caplog, event):
    return [r for r in caplog.records if getattr(r, "rtl_event", None) == event]


def _read(text, interest=CLOCK, source="x.sdc"):
    cmds, backend = read_commands(text, interest=interest, source=source)
    assert backend == "tokenizer"
    return cmds


# ---------------------------------------------------------------------------
# backend identity
# ---------------------------------------------------------------------------


def test_backend_name_is_tokenizer_today():
    # #641 adds a "tcl" interp behind the same call; until then this is the
    # value `rb tool-check` prints.
    assert backend_name() == "tokenizer"
    assert read_commands("", interest=CLOCK)[1] == backend_name()


# ---------------------------------------------------------------------------
# command shape
# ---------------------------------------------------------------------------


def test_plain_command_words_line_and_raw():
    [cmd] = _read("create_clock -name clk -period 10.0 [get_ports clk]\n")
    assert isinstance(cmd, TclCommand)
    assert cmd.name == "create_clock"
    assert cmd.words == ["-name", "clk", "-period", "10.0", "[get_ports clk]"]
    assert cmd.line == 1
    assert cmd.raw == "create_clock -name clk -period 10.0 [get_ports clk]"


def test_commands_outside_interest_are_dropped():
    text = (
        "set_property IOSTANDARD LVCMOS18 [get_ports clk]\n"
        "create_clock -name clk -period 10 [get_ports clk]\n"
        "create_pblock pblock_x\n"
    )
    assert [c.name for c in _read(text)] == ["create_clock"]


def test_line_numbers_count_physical_lines():
    text = (
        "# a comment\n"
        "\n"
        "create_clock -name a -period 1\n"
        "set_property X Y\n"
        "create_clock -name b -period 2\n"
    )
    assert [c.line for c in _read(text)] == [3, 5]


def test_continuation_is_one_command_reported_at_its_first_line():
    text = "# head\ncreate_clock -name clk \\\n  -period 10.0 [get_ports clk]\n"
    [cmd] = _read(text)
    assert cmd.words == ["-name", "clk", "-period", "10.0", "[get_ports clk]"]
    assert cmd.line == 2
    # raw is the reconstructed logical command, not the first physical line
    assert cmd.raw == "create_clock -name clk -period 10.0 [get_ports clk]"


def test_line_number_after_a_continued_command_stays_correct():
    text = "create_clock -name a \\\n  -period 1\ncreate_clock -name b -period 2\n"
    assert [c.line for c in _read(text)] == [1, 3]


def test_braced_value_is_one_word_kept_literal():
    [cmd] = _read("create_clock -name clk -period {10.0} [get_ports clk]\n")
    assert cmd.words == ["-name", "clk", "-period", "{10.0}", "[get_ports clk]"]


def test_trailing_comment_does_not_join_the_command():
    [cmd] = _read("create_clock -name clk -period {10.0} # not a word\n")
    assert cmd.words == ["-name", "clk", "-period", "{10.0}"]
    assert cmd.raw == "create_clock -name clk -period {10.0}"


def test_hash_inside_braces_is_not_a_comment():
    [cmd] = _read("create_clock -name clk#1 -period 10 [get_ports {clk#1}]\n")
    assert cmd.words == ["-name", "clk#1", "-period", "10", "[get_ports {clk#1}]"]


def test_nested_bracket_is_one_word():
    [cmd] = _read(
        "set_false_path -from [get_pins [get_cells u_a]/C] -to [get_cells u_b]\n",
        interest=frozenset({"set_false_path"}),
    )
    assert cmd.words == [
        "-from",
        "[get_pins [get_cells u_a]/C]",
        "-to",
        "[get_cells u_b]",
    ]


def test_newline_inside_a_brace_does_not_end_the_command():
    text = "set_clock_groups -asynchronous -group {a\nb} -group {c}\ncreate_clock -name z -period 1\n"
    cmds = _read(text, interest=frozenset({"set_clock_groups", "create_clock"}))
    assert [c.name for c in cmds] == ["set_clock_groups", "create_clock"]
    assert cmds[0].words == ["-asynchronous", "-group", "{a\nb}", "-group", "{c}"]
    assert cmds[0].line == 1
    assert cmds[1].line == 3
    # the reconstruction folds the embedded newline into a space
    assert cmds[0].raw == "set_clock_groups -asynchronous -group {a b} -group {c}"


def test_empty_text_reads_no_commands():
    assert _read("") == []
    assert _read("\n  # only a comment\n\n") == []


# ---------------------------------------------------------------------------
# the one-per-file out-of-scope warning
# ---------------------------------------------------------------------------


def test_variable_reference_warns_once_per_file(caplog):
    text = (
        "create_clock -name a -period $p [get_ports a]\n"
        "create_clock -name b -period $q [get_ports b]\n"
    )
    with caplog.at_level(logging.WARNING):
        cmds = _read(text, source="vars.sdc")
    # the commands are still returned — the warning says the reading may be
    # incomplete, it does not stop it
    assert [c.words[3] for c in cmds] == ["$p", "$q"]
    [rec] = _events(caplog, "constraints.tokenizer_skipped")
    assert rec.levelno == logging.WARNING
    assert rec.rtl_fields["source"] == "vars.sdc"
    assert rec.rtl_fields["line"] == 1
    assert rec.rtl_fields["trigger"] == "$p"
    assert "expr" in rec.rtl_fields["features"]
    assert "vars.sdc" in rec.getMessage()


def test_scripting_command_warns_once_and_names_the_line(caplog):
    text = "create_clock -name a -period 1\nset p 10\nproc f {} {}\n"
    with caplog.at_level(logging.WARNING):
        _read(text, source="s.sdc")
    [rec] = _events(caplog, "constraints.tokenizer_skipped")
    assert rec.rtl_fields["command"] == "set"
    assert rec.rtl_fields["line"] == 2


def test_ordinary_constraint_file_does_not_warn(caplog):
    # set_property / create_pblock are out of *interest*, not out of scope —
    # warning on them would fire on every real XDC.
    text = (
        "create_clock -name clk -period 10 [get_ports clk]\n"
        "set_property IOSTANDARD LVCMOS18 [get_ports clk]\n"
        "create_pblock pblock_x\n"
    )
    with caplog.at_level(logging.WARNING):
        _read(text)
    assert _events(caplog, "constraints.tokenizer_skipped") == []


def test_warning_is_per_call_not_per_process(caplog):
    text = "set p 10\ncreate_clock -name a -period $p\n"
    with caplog.at_level(logging.WARNING):
        _read(text, source="one.sdc")
        _read(text, source="two.sdc")
    sources = [
        r.rtl_fields["source"] for r in _events(caplog, "constraints.tokenizer_skipped")
    ]
    assert sources == ["one.sdc", "two.sdc"]


# ---------------------------------------------------------------------------
# extract_names (the rtl_buddy-side wrapper over the vendored peeler)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "word,expected",
    [
        ("clk_a", ["clk_a"]),
        ("{clk_a}", ["clk_a"]),
        ("{clk_a clk_b}", ["clk_a", "clk_b"]),
        ("[get_clocks clk_a]", ["clk_a"]),
        ("[get_clocks {clk_a clk_b}]", ["clk_a", "clk_b"]),
        ("[get_cells -hierarchical u_sync/*]", ["u_sync"]),
        # names before -filter survive; the predicate is dropped whole
        ("[get_cells u_sync/* -filter {IS_SEQUENTIAL}]", ["u_sync"]),
        # nested collection: the inner get_cells head and the `/C` pin
        # remainder are both dropped, leaving the instance
        ("[get_pins [get_cells u_a]/C]", ["u_a"]),
        ("", []),
    ],
)
def test_extract_names_shapes(word, expected):
    assert extract_names(word) == expected
