"""
Unit tests for the raw Verilator coverage database reader (#399).

Toggle and expression detail exists only in `coverage.dat`:
`verilator_coverage --write-info` folds both into anonymous `DA:` records.
Record shapes match Verilator 5.049 output.
"""

from rtl_buddy.cov.raw import (
    BRANCH,
    COVER,
    EXPRESSION,
    LINE,
    TOGGLE,
    canonical_metric,
    module_from_page,
    parse_raw_records,
    point_key,
)


def _record(
    *,
    file="../../tb_top.sv",
    line=89,
    col=17,
    name="APB_IF_WRITE",
    page=None,
    hier=None,
    count=13,
    type_="user",
):
    if page is None:
        page = f"v_{type_}/tb_top"
    if hier is None:
        hier = f"tb_top.{name}"
    keys = [
        ("f", file),
        ("l", str(line)),
        ("n", str(col)),
        ("t", type_),
        ("page", page),
        ("o", name),
        ("h", hier),
    ]
    blob = "".join(f"\x01{k}\x02{v}" for k, v in keys)
    return f"C '{blob}' {count}\n"


def _write_dat(tmp_path, records, name="coverage.dat"):
    path = tmp_path / name
    path.write_text("# SystemC::Coverage-3\n" + "".join(records), encoding="utf-8")
    return str(path)


def test_parses_every_record_type_with_canonical_metric_names(tmp_path):
    raw = _write_dat(
        tmp_path,
        [
            _record(type_="line", name="", line=10, count=4),
            _record(type_="branch", name="if", line=11, count=0),
            _record(type_="toggle", name="q[3]", line=12, count=7),
            _record(type_="expr", name="a && b", line=13, count=1),
            _record(type_="user", name="APB_IF_WRITE", line=14, count=3),
        ],
    )

    metrics = [record["metric"] for record in parse_raw_records(raw)]

    assert metrics == [LINE, BRANCH, TOGGLE, EXPRESSION, COVER]


def test_toggle_records_keep_the_signal_name_and_module(tmp_path):
    raw = _write_dat(
        tmp_path, [_record(type_="toggle", name="q[3]", line=12, col=5, count=7)]
    )

    (record,) = parse_raw_records(raw, metrics=[TOGGLE])

    assert record == {
        "metric": TOGGLE,
        "type": "toggle",
        "name": "q[3]",
        "file": "../../tb_top.sv",
        "line": 12,
        "column": 5,
        "module": "tb_top",
        "hier": "tb_top.q[3]",
        "hits": 7,
    }


def test_metrics_filter_selects_one_type(tmp_path):
    raw = _write_dat(
        tmp_path,
        [
            _record(type_="toggle", name="q[0]"),
            _record(type_="line", name=""),
        ],
    )

    assert [r["metric"] for r in parse_raw_records(raw, metrics=[TOGGLE])] == [TOGGLE]


def test_a_quote_in_the_comment_does_not_truncate_the_record(tmp_path):
    raw = _write_dat(
        tmp_path, [_record(type_="expr", name="don't care", line=20, count=2)]
    )

    (record,) = parse_raw_records(raw, metrics=[EXPRESSION])

    assert record["name"] == "don't care"
    assert record["hits"] == 2


def test_unreadable_database_reports_none_not_empty(tmp_path):
    assert parse_raw_records(str(tmp_path / "nope.dat")) is None


def test_unknown_record_types_are_skipped(tmp_path):
    raw = _write_dat(tmp_path, [_record(type_="something-new")])

    assert parse_raw_records(raw) == []


def test_canonical_metric_and_page_helpers():
    assert canonical_metric("user") == COVER
    assert canonical_metric("expression") == EXPRESSION
    assert canonical_metric("nope") is None
    assert module_from_page("v_toggle/blk") == "blk"
    assert module_from_page("v_user/tb_top") == "tb_top"
    assert module_from_page("something_else") == "something_else"
    assert module_from_page(None) is None


def test_line_points_key_on_the_line_alone_others_on_the_full_identity():
    line_record = {"metric": LINE, "line": 7, "column": 1, "name": None, "module": "a"}
    toggle_record = {
        "metric": TOGGLE,
        "line": 7,
        "column": 1,
        "name": "q[0]",
        "module": "a",
    }

    assert point_key(line_record) == (7,)
    assert point_key(toggle_record) == (7, 1, "q[0]", "a")
