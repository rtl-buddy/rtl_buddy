"""Contract tests for the Tcl constraint reader.

Two backends answer :func:`read_commands`: the ``tcl`` safe interp
(rtl-buddy/rtl_buddy#641) and the vendored word tokenizer (#642). Three
kinds of test live here:

* **Shared contract** — parametrised over both backends via the
  ``constraint_backend`` fixture. Every row of #642's reproduction tables
  (continuation, braced values, nested collections, ``#`` inside braces)
  belongs here: a consumer must not care which backend answered.
* **Backend-specific** — what only an interpreter can do (``$p``,
  ``[expr]``, ``;`` separators, and the safety/limit properties that come
  with evaluating a file), and the literal word shapes the tokenizer
  hands back plus its one-warning-per-file contract.
* **Equivalence** — the same fixture set read both ways, compared at the
  level a consumer reads it.
"""

from __future__ import annotations

import logging
import time

import pytest

from rtl_buddy.constraints import tcl_reader
from rtl_buddy.constraints.tcl_reader import (
    TclCommand,
    backend_description,
    backend_name,
    extract_names,
    read_commands,
)
from rtl_buddy.errors import FatalRtlBuddyError

CLOCK = frozenset({"create_clock"})
SDC = frozenset(
    {
        "create_clock",
        "set_clock_groups",
        "set_false_path",
        "set_max_delay",
        "set_property",
    }
)


def _events(caplog, event):
    return [r for r in caplog.records if getattr(r, "rtl_event", None) == event]


def _read(text, interest=CLOCK, source="x.sdc", backend=None):
    cmds, used = read_commands(text, interest=interest, source=source)
    if backend is not None:
        assert used == backend
    return cmds


def _canon(word: str) -> tuple[str, ...]:
    """What a consumer can read out of one word, backend-independently.

    The two backends do not produce byte-identical words — Tcl strips the
    braces off ``{10.0}`` and evaluates ``[expr]`` — but every consumer
    reads a target through :func:`extract_names`, which peels a brace
    group, a bracket span and a bare multi-word string the same way. That
    is the contract both backends must meet.
    """
    if word[:1] in "[{" or any(ch.isspace() for ch in word):
        return tuple(extract_names(word))
    return (word,)


def _shape(cmd: TclCommand) -> tuple:
    return (cmd.name, cmd.line, tuple(_canon(w) for w in cmd.words))


# ---------------------------------------------------------------------------
# backend selection
# ---------------------------------------------------------------------------


def test_backend_name_follows_the_env_override(constraint_backend):
    assert backend_name() == constraint_backend
    assert read_commands("", interest=CLOCK)[1] == constraint_backend


def test_the_interp_is_the_default_when_tkinter_imports(monkeypatch):
    monkeypatch.delenv(tcl_reader.BACKEND_ENV, raising=False)
    expected = "tcl" if tcl_reader.tkinter_available() else "tokenizer"
    assert backend_name() == expected


def test_missing_tkinter_falls_back_and_warns_once_per_process(monkeypatch, caplog):
    monkeypatch.delenv(tcl_reader.BACKEND_ENV, raising=False)
    monkeypatch.setattr(tcl_reader, "_TKINTER_ERROR", "No module named '_tkinter'")
    monkeypatch.setattr(tcl_reader, "_UNAVAILABLE_LOGGED", False)
    with caplog.at_level(logging.WARNING):
        assert backend_name() == "tokenizer"
        cmds, used = read_commands(
            "create_clock -name clk -period 10\n", interest=CLOCK
        )
    assert used == "tokenizer"
    assert [c.name for c in cmds] == ["create_clock"]
    # the interpreter is a property of the process, not of the file, so
    # reading ten SDCs must not print this ten times
    [rec] = _events(caplog, "constraints.tcl_unavailable")
    assert "_tkinter" in rec.rtl_fields["error"]
    assert any("uv python install" in h for h in rec.rtl_fields["hints"])
    message = rec.getMessage()
    assert "brew install python-tk" in message
    assert "dnf install python3-tkinter" in message


def test_forcing_the_interp_without_tkinter_is_fatal(monkeypatch):
    # A pinned backend that silently answers with the other one is not a
    # pin. Say so instead.
    monkeypatch.setenv(tcl_reader.BACKEND_ENV, "tcl")
    monkeypatch.setattr(tcl_reader, "_TKINTER_ERROR", "No module named '_tkinter'")
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        backend_name()
    assert "brew install python-tk" in str(excinfo.value)


def test_an_unknown_override_is_fatal(monkeypatch):
    monkeypatch.setenv(tcl_reader.BACKEND_ENV, "regex")
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        read_commands("", interest=CLOCK)
    assert "'tcl'" in str(excinfo.value)


def test_the_override_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv(tcl_reader.BACKEND_ENV, "  TOKENIZER ")
    assert backend_name() == "tokenizer"


def test_backend_description_names_the_tcl_version(tcl_backend):
    # `rb tool-check` prints this: which reader answered, and which Tcl.
    description = backend_description()
    assert description.startswith("tcl (Tcl ") and description.endswith(")")


def test_backend_description_is_the_plain_name_for_the_tokenizer(tokenizer_backend):
    assert backend_description() == "tokenizer"


# ---------------------------------------------------------------------------
# the shared contract: same reading from either backend
# ---------------------------------------------------------------------------


def test_plain_command_words_line_and_raw(constraint_backend):
    [cmd] = _read(
        "create_clock -name clk -period 10.0 [get_ports clk]\n",
        backend=constraint_backend,
    )
    assert isinstance(cmd, TclCommand)
    assert cmd.name == "create_clock"
    assert _shape(cmd) == (
        "create_clock",
        1,
        (("-name",), ("clk",), ("-period",), ("10.0",), ("clk",)),
    )
    assert cmd.raw == "create_clock -name clk -period 10.0 [get_ports clk]"


def test_commands_outside_interest_are_dropped(constraint_backend):
    text = (
        "set_property IOSTANDARD LVCMOS18 [get_ports clk]\n"
        "create_clock -name clk -period 10 [get_ports clk]\n"
        "create_pblock pblock_x\n"
    )
    assert [c.name for c in _read(text, backend=constraint_backend)] == ["create_clock"]


def test_line_numbers_count_physical_lines(constraint_backend):
    text = (
        "# a comment\n"
        "\n"
        "create_clock -name a -period 1\n"
        "set_property X Y\n"
        "create_clock -name b -period 2\n"
    )
    assert [c.line for c in _read(text, backend=constraint_backend)] == [3, 5]


def test_continuation_is_one_command_reported_at_its_first_line(constraint_backend):
    text = "# head\ncreate_clock -name clk \\\n  -period 10.0 [get_ports clk]\n"
    [cmd] = _read(text, backend=constraint_backend)
    assert _canon(cmd.words[3]) == ("10.0",)
    assert cmd.line == 2
    # raw is the logical command, not the first physical line
    assert cmd.raw == "create_clock -name clk -period 10.0 [get_ports clk]"


def test_line_number_after_a_continued_command_stays_correct(constraint_backend):
    text = "create_clock -name a \\\n  -period 1\ncreate_clock -name b -period 2\n"
    assert [c.line for c in _read(text, backend=constraint_backend)] == [1, 3]


def test_braced_value_is_one_word(constraint_backend):
    [cmd] = _read(
        "create_clock -name clk -period {10.0} [get_ports clk]\n",
        backend=constraint_backend,
    )
    assert _canon(cmd.words[3]) == ("10.0",)
    assert _canon(cmd.words[4]) == ("clk",)


def test_trailing_comment_leaves_the_period_readable(constraint_backend):
    # `#` starts a comment only at the start of a command in real Tcl, so
    # the interp hands the trailing words to `create_clock` while the
    # tokenizer drops them (each backend's own test below pins that). What
    # both must agree on is the part a consumer reads.
    [cmd] = _read(
        "create_clock -name clk -period {10.0} # not a word\n",
        backend=constraint_backend,
    )
    assert [_canon(w) for w in cmd.words[:4]] == [
        ("-name",),
        ("clk",),
        ("-period",),
        ("10.0",),
    ]


def test_hash_inside_braces_is_not_a_comment(constraint_backend):
    [cmd] = _read(
        "create_clock -name clk#1 -period 10 [get_ports {clk#1}]\n",
        backend=constraint_backend,
    )
    assert cmd.words[1] == "clk#1"
    assert _canon(cmd.words[4]) == ("clk#1",)


def test_nested_bracket_is_one_target(constraint_backend):
    [cmd] = _read(
        "set_false_path -from [get_pins [get_cells u_a]/C] -to [get_cells u_b]\n",
        interest=frozenset({"set_false_path"}),
        backend=constraint_backend,
    )
    assert [_canon(w) for w in cmd.words] == [
        ("-from",),
        ("u_a",),
        ("-to",),
        ("u_b",),
    ]


def test_newline_inside_a_brace_does_not_end_the_command(constraint_backend):
    text = (
        "set_clock_groups -asynchronous -group {a\nb} -group {c}\n"
        "create_clock -name z -period 1\n"
    )
    cmds = _read(text, interest=SDC, backend=constraint_backend)
    assert [c.name for c in cmds] == ["set_clock_groups", "create_clock"]
    assert [_canon(w) for w in cmds[0].words] == [
        ("-asynchronous",),
        ("-group",),
        ("a", "b"),
        ("-group",),
        ("c",),
    ]
    assert cmds[0].line == 1
    assert cmds[1].line == 3
    # the reconstruction folds the embedded newline into a space
    assert "-group {a b}" in cmds[0].raw


def test_empty_text_reads_no_commands(constraint_backend):
    assert _read("", backend=constraint_backend) == []
    assert _read("\n  # only a comment\n\n", backend=constraint_backend) == []


def test_ordinary_constraint_file_reads_clean(constraint_backend, caplog):
    # set_property / create_pblock are out of *interest*, not out of scope —
    # warning on them would fire on every real XDC.
    text = (
        "create_clock -name clk -period 10 [get_ports clk]\n"
        "set_property IOSTANDARD LVCMOS18 [get_ports clk]\n"
        "create_pblock pblock_x\n"
    )
    with caplog.at_level(logging.WARNING):
        assert len(_read(text, backend=constraint_backend)) == 1
    assert _events(caplog, "constraints.tokenizer_skipped") == []
    assert _events(caplog, "constraints.tcl_error") == []


# ---------------------------------------------------------------------------
# the interp backend: what only evaluation can do
# ---------------------------------------------------------------------------


def test_variables_and_expr_are_evaluated(tcl_backend):
    [cmd] = _read(
        "set p 10\ncreate_clock -name clk -period [expr {$p*2}] [get_ports clk]\n",
        backend="tcl",
    )
    assert cmd.words == ["-name", "clk", "-period", "20", "[get_ports clk]"]
    assert cmd.line == 2


def test_semicolons_separate_commands_under_the_interp(tcl_backend):
    # Pinned as a *difference*, not fixed on the tokenizer side: `;` is a
    # command separator only to a real parser, and the vendored tokenizer
    # is a verbatim copy that must not grow rules here.
    cmds = _read(
        "create_clock -name a -period 1; create_clock -name b -period 2\n",
        backend="tcl",
    )
    assert [c.words[1] for c in cmds] == ["a", "b"]
    assert [c.line for c in cmds] == [1, 1]


def test_semicolons_are_ordinary_characters_to_the_tokenizer(tokenizer_backend):
    [cmd] = _read(
        "create_clock -name a -period 1; create_clock -name b -period 2\n",
        backend="tokenizer",
    )
    assert cmd.words[3] == "1;"


def test_a_mid_command_hash_is_a_word_to_tcl_not_a_comment(tcl_backend):
    # Real Tcl only starts a comment where a command starts. Vivado reads
    # SDC with a real Tcl parser, so this is the faithful reading; the
    # idiomatic way to write the same comment is `;#`, which is read as a
    # comment by the interp (below).
    [cmd] = _read("create_clock -name clk -period 10 # main clock\n", backend="tcl")
    assert cmd.words[4:] == ["#", "main", "clock"]


def test_a_semicolon_comment_is_a_comment(tcl_backend):
    [cmd] = _read("create_clock -name clk -period 10 ;# main clock\n", backend="tcl")
    assert cmd.words == ["-name", "clk", "-period", "10"]


def test_state_does_not_leak_between_files(tcl_backend, caplog):
    # A fresh child interp per call: `set p 10` in one SDC must not make
    # `$p` readable in the next one.
    _read("set p 10\ncreate_clock -name a -period $p\n", backend="tcl")
    with caplog.at_level(logging.WARNING):
        cmds, used = read_commands(
            "create_clock -name b -period $p\n", interest=CLOCK, source="second.sdc"
        )
    # unreadable variable -> Tcl refuses the file -> the tokenizer answers
    assert used == "tokenizer"
    assert cmds[0].words[3] == "$p"
    [rec] = _events(caplog, "constraints.tcl_error")
    assert rec.rtl_fields["source"] == "second.sdc"
    assert rec.rtl_fields["line"] == 1
    assert "no such variable" in rec.rtl_fields["message"]


def test_a_tcl_syntax_error_falls_back_to_the_tokenizer(tcl_backend, caplog):
    with caplog.at_level(logging.WARNING):
        cmds, used = read_commands(
            "create_clock -name a -period 1\ncreate_clock -name b -period {2\n",
            interest=CLOCK,
            source="broken.sdc",
        )
    assert used == "tokenizer"
    # the readable clock is still read: a file Tcl will not run is still a
    # file whose create_clock lines abc needs
    assert cmds[0].words[1] == "a"
    [rec] = _events(caplog, "constraints.tcl_error")
    assert "close-brace" in rec.rtl_fields["message"]


def test_source_includes_are_reported_as_unsupported(tcl_backend, caplog):
    with caplog.at_level(logging.WARNING):
        cmds = _read(
            "source shared/clocks.sdc\ncreate_clock -name clk -period 10\n",
            source="top.sdc",
            backend="tcl",
        )
    # `source` is absent from a safe interp, so it lands in the recorder
    # rather than reading a file — and the reader says so instead of
    # silently reading half a design's constraints.
    [rec] = _events(caplog, "constraints.include_unsupported")
    assert rec.rtl_fields["included"] == "shared/clocks.sdc"
    assert rec.rtl_fields["line"] == 1
    assert "not supported" in rec.getMessage()
    assert [c.name for c in cmds] == ["create_clock"]


def test_a_hostile_file_cannot_touch_the_filesystem_or_spawn(tcl_backend, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    marker = tmp_path / "spawned"
    text = (
        f'exec /bin/sh -c "rm -f {victim}; touch {marker}"\n'
        f"set fh [open {victim} w]\n"
        f"file delete {victim}\n"
        f"cd {tmp_path}\n"
        "pwd\n"
        "glob *\n"
        "socket localhost 22\n"
        "load /usr/lib/libc.so\n"
        "create_clock -name clk -period 10 [get_ports clk]\n"
    )
    cmds, used = read_commands(
        text, interest=frozenset({"exec", "create_clock"}), source="hostile.sdc"
    )
    assert used == "tcl"
    # every dangerous command is a plain unknown command in a safe interp
    assert victim.read_text() == "keep"
    assert not marker.exists()
    assert [c.name for c in cmds] == ["exec", "create_clock"]


def test_a_blocking_command_cannot_sleep_the_reader(tcl_backend):
    started = time.monotonic()
    cmds = _read("after 30000\ncreate_clock -name clk -period 10\n", backend="tcl")
    assert time.monotonic() - started < 5
    assert [c.name for c in cmds] == ["create_clock"]


def test_a_spinning_file_hits_the_command_limit(tcl_backend, caplog, monkeypatch):
    monkeypatch.setattr(tcl_reader, "TCL_COMMAND_LIMIT", 5000)
    with caplog.at_level(logging.WARNING):
        cmds, used = read_commands(
            "create_clock -name a -period 1\nwhile 1 {set x 1}\n",
            interest=CLOCK,
            source="spin.sdc",
        )
    [rec] = _events(caplog, "constraints.tcl_error")
    assert "command count limit" in rec.rtl_fields["message"]
    # bounded, and the file is still read by the fallback
    assert used == "tokenizer"
    assert [c.name for c in cmds] == ["create_clock"]


def test_an_idle_infinite_loop_hits_the_time_limit(tcl_backend, caplog, monkeypatch):
    # `while 1 {}` runs no commands at all, so only the wall-clock limit
    # stops it. Without one this call never returns.
    monkeypatch.setattr(tcl_reader, "TCL_TIME_LIMIT_SECONDS", 1)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        _, used = read_commands("while 1 {}\n", interest=CLOCK, source="hang.sdc")
    assert time.monotonic() - started < 30
    assert used == "tokenizer"
    [rec] = _events(caplog, "constraints.tcl_error")
    assert "time limit" in rec.rtl_fields["message"]


# ---------------------------------------------------------------------------
# the tokenizer backend: literal words and the one-per-file warning
# ---------------------------------------------------------------------------


def test_tokenizer_keeps_words_literal(tokenizer_backend):
    [cmd] = _read(
        "create_clock -name clk -period {10.0} [get_ports clk]\n", backend="tokenizer"
    )
    assert cmd.words == ["-name", "clk", "-period", "{10.0}", "[get_ports clk]"]


def test_tokenizer_keeps_a_nested_collection_whole(tokenizer_backend):
    [cmd] = _read(
        "set_false_path -from [get_pins [get_cells u_a]/C] -to [get_cells u_b]\n",
        interest=frozenset({"set_false_path"}),
        backend="tokenizer",
    )
    assert cmd.words == [
        "-from",
        "[get_pins [get_cells u_a]/C]",
        "-to",
        "[get_cells u_b]",
    ]


def test_tokenizer_drops_a_trailing_comment(tokenizer_backend):
    # The forgiving reading, and the one #642 shipped: everything from a
    # word-boundary `#` to end of line goes.
    [cmd] = _read(
        "create_clock -name clk -period 10 # main clock\n", backend="tokenizer"
    )
    assert cmd.words == ["-name", "clk", "-period", "10"]
    assert cmd.raw == "create_clock -name clk -period 10"


def test_variable_reference_warns_once_per_file(tokenizer_backend, caplog):
    text = (
        "create_clock -name a -period $p [get_ports a]\n"
        "create_clock -name b -period $q [get_ports b]\n"
    )
    with caplog.at_level(logging.WARNING):
        cmds = _read(text, source="vars.sdc", backend="tokenizer")
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


def test_scripting_command_warns_once_and_names_the_line(tokenizer_backend, caplog):
    text = "create_clock -name a -period 1\nset p 10\nproc f {} {}\n"
    with caplog.at_level(logging.WARNING):
        _read(text, source="s.sdc", backend="tokenizer")
    [rec] = _events(caplog, "constraints.tokenizer_skipped")
    assert rec.rtl_fields["command"] == "set"
    assert rec.rtl_fields["line"] == 2


def test_warning_is_per_call_not_per_process(tokenizer_backend, caplog):
    text = "set p 10\ncreate_clock -name a -period $p\n"
    with caplog.at_level(logging.WARNING):
        _read(text, source="one.sdc", backend="tokenizer")
        _read(text, source="two.sdc", backend="tokenizer")
    sources = [
        r.rtl_fields["source"] for r in _events(caplog, "constraints.tokenizer_skipped")
    ]
    assert sources == ["one.sdc", "two.sdc"]


# ---------------------------------------------------------------------------
# cross-backend equivalence over the whole fixture set
# ---------------------------------------------------------------------------


EQUIVALENT_FIXTURES = {
    "plain": "create_clock -name clk -period 10.0 [get_ports clk]\n",
    "continuation": (
        "create_clock -name clk \\\n  -period 10.0 \\\n  [get_ports clk]\n"
    ),
    "braced_period": "create_clock -name clk -period {10.0} [get_ports clk]\n",
    "nested_collection": (
        "set_false_path -from [get_pins [get_cells u_a]/C] -to [get_cells u_b]\n"
    ),
    "hash_in_braces": "create_clock -name clk#1 -period 10 [get_ports {clk#1}]\n",
    "filter": (
        "set_max_delay -datapath_only 5 -from [get_cells u_sync/* "
        "-filter {IS_SEQUENTIAL}] -to [get_cells u_dst]\n"
    ),
    "clock_groups": (
        "set_clock_groups -asynchronous -group {a b} -group [get_clocks c]\n"
    ),
    "brace_over_lines": "set_clock_groups -asynchronous -group {a\n b} -group {c}\n",
    "comments_and_blanks": (
        "# header\n\ncreate_clock -name a -period 1\n"
        "#create_clock -name dead -period 99\ncreate_clock -name b -period 2\n"
    ),
    "quoted_word": 'create_clock -name "clk main" -period 10\n',
    "unknown_commands": (
        "set_property IOSTANDARD LVCMOS18 [get_ports clk]\n"
        "create_pblock p0\ncreate_clock -name clk -period 10 [get_ports clk]\n"
    ),
    "empty": "",
}


@pytest.mark.parametrize("fixture", sorted(EQUIVALENT_FIXTURES))
def test_both_backends_read_the_same_constraints(fixture, monkeypatch):
    """Neither consumer may care which backend answered.

    Only files inside the tokenizer's scope are compared — a ``$var`` or
    an ``[expr]`` is exactly where the two are *meant* to differ, and
    those differences are pinned in the backend-specific tests above.
    """
    if not tcl_reader.tkinter_available():
        pytest.skip("this Python has no _tkinter, so there is nothing to compare")
    text = EQUIVALENT_FIXTURES[fixture]
    read = {}
    for backend in ("tcl", "tokenizer"):
        monkeypatch.setenv(tcl_reader.BACKEND_ENV, backend)
        cmds, used = read_commands(text, interest=SDC, source=f"{fixture}.sdc")
        assert used == backend
        read[backend] = [_shape(c) for c in cmds]
    assert read["tcl"] == read["tokenizer"]


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
        # the shape the interp's recorder rebuilds for the same input
        ("[get_pins {[get_cells u_a]/C}]", ["u_a"]),
        ("", []),
    ],
)
def test_extract_names_shapes(word, expected):
    assert extract_names(word) == expected
