"""Tests for the vendored Tcl-aware word tokenizer.

Copied from rtl-buddy-cdc's ``tests/test_sdc_tokenizer.py`` at the commit pinned for :mod:`rtl_buddy.constraints.tcl_tokenizer` (``df996d55af0e8b824183d03d531426b8601af429``); only the import path differs. The upstream test that checks the cdc ``sdc`` module re-exports ``_tokenize`` is omitted because it has no counterpart here.
"""

from __future__ import annotations

from rtl_buddy.constraints.tcl_tokenizer import _extract_names, _tokenize


def test_plain_command() -> None:
    assert _tokenize("create_clock -name clk -period 10") == [
        ["create_clock", "-name", "clk", "-period", "10"],
    ]


def test_brace_collection_is_one_word() -> None:
    """``{a b}`` is a single word."""
    assert _tokenize("set_clock_groups -group {ck_a ck_b}") == [
        ["set_clock_groups", "-group", "{ck_a ck_b}"],
    ]


def test_bracket_collection_is_one_word() -> None:
    """``[get_ports clk]`` is a single word."""
    assert _tokenize("create_clock -name clk [get_ports clk]") == [
        ["create_clock", "-name", "clk", "[get_ports clk]"],
    ]


def test_single_token_brace_is_one_word() -> None:
    """``{ck_a}`` without internal whitespace is a single word."""
    assert _tokenize("-source {ck_a}") == [["-source", "{ck_a}"]]


def test_single_token_bracket_is_one_word() -> None:
    """``[ck_a]`` without internal whitespace is a single word."""
    assert _tokenize("-source [ck_a]") == [["-source", "[ck_a]"]]


def test_nested_braces_respected() -> None:
    """A ``{...}`` word containing nested braces ends at the matching outer ``}``."""
    [words] = _tokenize("set_x {{a b} {c d}}")
    assert words == ["set_x", "{{a b} {c d}}"]


def test_nested_brackets_respected() -> None:
    """``[outer [inner] tail]`` is one word."""
    [words] = _tokenize("set_x [foo [bar] baz]")
    assert words == ["set_x", "[foo [bar] baz]"]


def test_braces_inside_brackets_do_not_terminate_bracket() -> None:
    """A ``}`` inside a bracket word does not close the bracket scan."""
    [words] = _tokenize('[get_ports -filter {NAME =~ "clk*"}]')
    assert words == ['[get_ports -filter {NAME =~ "clk*"}]']


def test_line_continuation_collapses_to_whitespace() -> None:
    """A backslash-newline collapses to inter-word whitespace."""
    src = "create_clock -name foo \\\n    -period 10 [get_ports foo]"
    assert _tokenize(src) == [
        ["create_clock", "-name", "foo", "-period", "10", "[get_ports foo]"],
    ]


def test_comment_skips_to_end_of_line() -> None:
    """``#`` at a word boundary starts a comment to end of line and flushes any partial command."""
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
    """``"..."`` is one word with the quotes stripped; a backslash escapes the next character."""
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
    """Repeated ``-group`` flags tokenize as distinct words in order."""
    [words] = _tokenize("set_clock_groups -group {a} -group {b}")
    assert words == ["set_clock_groups", "-group", "{a}", "-group", "{b}"]


def test_empty_input_yields_no_commands() -> None:
    assert _tokenize("") == []
    assert _tokenize("   \n  \t  \n") == []


def test_brace_with_spaces_at_edges() -> None:
    """``{ ck_a }`` is one word; handlers strip the inner whitespace."""
    [words] = _tokenize("-group { ck_a }")
    assert words == ["-group", "{ ck_a }"]


# _extract_names


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
