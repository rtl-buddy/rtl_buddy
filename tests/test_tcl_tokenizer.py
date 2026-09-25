"""Unit tests for the vendored Tcl-aware word tokenizer (#642).

Copied from rtl-buddy-cdc's ``tests/test_sdc_tokenizer.py`` at the same
pinned commit as :mod:`rtl_buddy.constraints.tcl_tokenizer` itself
(``53b5f34161debccda45c44a33c9b663ee5410b1a``); only the import path is
adapted. Upstream's own rationale follows.

The tokenizer is the foundation of the issue #144 parser refactor —
every collection-handling bug we fixed in #140 / #142 ultimately
traced to mis-counting tokens emitted by ``shlex``. These tests pin
the tokenizer's behaviour directly so a regression here would surface
as a focused failure rather than a downstream parser mystery.
"""

from __future__ import annotations

from rtl_buddy.constraints.tcl_tokenizer import _extract_names, _tokenize


def test_plain_command() -> None:
    assert _tokenize("create_clock -name clk -period 10") == [
        ["create_clock", "-name", "clk", "-period", "10"],
    ]


def test_brace_collection_is_one_word() -> None:
    """``{a b}`` is a single Tcl word — the whole point of the rewrite.

    With the old ``shlex`` layer this came back as ``["{a", "b}"]`` and
    forced the per-command handlers to re-stitch tokens."""
    assert _tokenize("set_clock_groups -group {ck_a ck_b}") == [
        ["set_clock_groups", "-group", "{ck_a ck_b}"],
    ]


def test_bracket_collection_is_one_word() -> None:
    """Same contract for ``[get_ports clk]`` — one word, not two."""
    assert _tokenize("create_clock -name clk [get_ports clk]") == [
        ["create_clock", "-name", "clk", "[get_ports clk]"],
    ]


def test_single_token_brace_is_one_word() -> None:
    """The shape that triggered #142: ``{ck_a}`` with no internal
    whitespace stays one word."""
    assert _tokenize("-source {ck_a}") == [["-source", "{ck_a}"]]


def test_single_token_bracket_is_one_word() -> None:
    """Sibling case to the brace form above."""
    assert _tokenize("-source [ck_a]") == [["-source", "[ck_a]"]]


def test_nested_braces_respected() -> None:
    """A ``{...}`` word containing another ``{...}`` must scan to the
    *matching* outer ``}``, not the first one. Real-world driver:
    Tcl-style nested filter expressions like
    ``[get_ports -filter {NAME =~ "clk*"}]``."""
    [words] = _tokenize("set_x {{a b} {c d}}")
    assert words == ["set_x", "{{a b} {c d}}"]


def test_nested_brackets_respected() -> None:
    """``[outer [inner] tail]`` is one word."""
    [words] = _tokenize("set_x [foo [bar] baz]")
    assert words == ["set_x", "[foo [bar] baz]"]


def test_braces_inside_brackets_do_not_terminate_bracket() -> None:
    """Mixing: ``[get_ports -filter {NAME =~ "clk*"}]`` — the ``}``
    inside the bracket must not close the bracket scan. Regression
    pin for ``test_filter_clause_is_partial_warning`` in test_sdc.py."""
    [words] = _tokenize('[get_ports -filter {NAME =~ "clk*"}]')
    assert words == ['[get_ports -filter {NAME =~ "clk*"}]']


def test_line_continuation_collapses_to_whitespace() -> None:
    """``\\<newline>`` is the SDC line-continuation marker; the
    tokenizer collapses it to inter-word whitespace so the multi-
    line command parses as one."""
    src = "create_clock -name foo \\\n    -period 10 [get_ports foo]"
    assert _tokenize(src) == [
        ["create_clock", "-name", "foo", "-period", "10", "[get_ports foo]"],
    ]


def test_comment_skips_to_end_of_line() -> None:
    """``#`` at a word boundary starts a comment to end-of-line. Any
    partial command in progress is flushed (matches the existing
    ``_logical_lines`` "comments break continuation" behaviour)."""
    src = "create_clock -name a -period 10 # the rest is comment\nset_x bar"
    assert _tokenize(src) == [
        ["create_clock", "-name", "a", "-period", "10"],
        ["set_x", "bar"],
    ]


def test_blank_lines_and_comment_only_lines_ignored() -> None:
    src = """
    # leading comment
    create_clock -name a -period 1

    # middle comment
    create_clock -name b -period 2
    """
    assert _tokenize(src) == [
        ["create_clock", "-name", "a", "-period", "1"],
        ["create_clock", "-name", "b", "-period", "2"],
    ]


def test_double_quoted_word_strips_quotes() -> None:
    """``"..."`` is one word; the quotes are stripped and backslash
    escapes the next character inside."""
    src = 'set_name "hello world" "with \\"embedded\\" quotes"'
    [words] = _tokenize(src)
    assert words == ["set_name", "hello world", 'with "embedded" quotes']


def test_multiple_commands_on_separate_lines() -> None:
    src = "create_clock -name a -period 1\ncreate_clock -name b -period 2"
    assert _tokenize(src) == [
        ["create_clock", "-name", "a", "-period", "1"],
        ["create_clock", "-name", "b", "-period", "2"],
    ]


def test_repeated_flag_words_preserved_in_order() -> None:
    """Two ``-group`` clauses tokenize as two distinct words; the
    arg-spec layer is what stitches them into a list of occurrences."""
    [words] = _tokenize("set_clock_groups -group {a} -group {b}")
    assert words == ["set_clock_groups", "-group", "{a}", "-group", "{b}"]


def test_empty_input_yields_no_commands() -> None:
    assert _tokenize("") == []
    assert _tokenize("   \n  \t  \n") == []


def test_brace_with_spaces_at_edges() -> None:
    """``{ ck_a }`` (with leading/trailing whitespace inside the
    braces) is still one word — handlers strip the whitespace on
    extraction."""
    [words] = _tokenize("-group { ck_a }")
    assert words == ["-group", "{ ck_a }"]


# ---------------------------------------------------------------------------
# _extract_names — the other half of the vendored copy (upstream exercises it
# through the parser's own tests, so these pin it directly here).
# ---------------------------------------------------------------------------


def test_extract_names_bare_identifier() -> None:
    assert _extract_names("clk") == (["clk"], False)


def test_extract_names_brace_collection() -> None:
    assert _extract_names("{ck0 ck1}") == (["ck0", "ck1"], False)


def test_extract_names_strips_get_head() -> None:
    assert _extract_names("[get_ports ck0 ck1]") == (["ck0", "ck1"], False)
    assert _extract_names("[get_clocks {clk_a clk_b}]") == (["clk_a", "clk_b"], False)


def test_extract_names_drops_flags() -> None:
    assert _extract_names("[get_pins -hierarchical u_a/C]") == (["u_a/C"], False)


def test_extract_names_reports_filter_and_drops_its_predicate() -> None:
    names, saw_filter = _extract_names('[get_ports -filter {NAME =~ "clk*"}]')
    assert saw_filter is True
    assert names == []
