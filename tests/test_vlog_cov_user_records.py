"""
Unit tests for per-cover-point (user coverage) extraction from raw databases.

A labeled SVA `cover property` lands in Verilator's `coverage.dat` as a
`t=user` record carrying the label in its comment key. The LCOV export folds
those into anonymous `DA:` records, so the raw database is the only place the
names survive (#367).
"""

from rtl_buddy.tools.vlog_cov import VlogCov, aggregate_cover_records


def _make_cov():
    return VlogCov(simulator_name="verilator", use_lcov=True)


def _record(
    *,
    file="../../tb_top.sv",
    line=89,
    name="APB_IF_WRITE",
    hier=None,
    count=13,
    type_="user",
):
    """Build one raw `C '...' <count>` record in Verilator's key encoding."""
    if hier is None:
        hier = f"tb_top.{name}"
    keys = [
        ("f", file),
        ("l", str(line)),
        ("t", type_),
        ("page", "v_user/tb_top"),
        ("o", name),
        ("h", hier),
    ]
    blob = "".join(f"\x01{k}\x02{v}" for k, v in keys)
    return f"C '{blob}' {count}\n"


def _write_dat(tmp_path, records, name="coverage.dat"):
    path = tmp_path / name
    path.write_text("# SystemC::Coverage-3\n" + "".join(records), encoding="utf-8")
    return str(path)


def test_parses_name_file_line_and_hits(tmp_path):
    raw = _write_dat(tmp_path, [_record()])

    records = _make_cov().parse_user_cover_records(raw)

    assert records == [
        {
            "name": "APB_IF_WRITE",
            "file": "../../tb_top.sv",
            "line": 89,
            "hier": "tb_top.APB_IF_WRITE",
            "hits": 13,
        }
    ]


def test_ignores_non_user_records(tmp_path):
    raw = _write_dat(
        tmp_path,
        [
            _record(name="COVER_A"),
            _record(name="line_thing", type_="line", count=4),
        ],
    )

    records = _make_cov().parse_user_cover_records(raw)

    assert [r["name"] for r in records] == ["COVER_A"]


def test_zero_hit_point_is_reported_not_dropped(tmp_path):
    raw = _write_dat(tmp_path, [_record(name="NEVER_HIT", count=0)])

    records = _make_cov().parse_user_cover_records(raw)

    assert records[0]["hits"] == 0


def test_falls_back_to_last_hierarchy_segment_when_comment_key_absent(tmp_path):
    blob = "\x01f\x02tb.sv\x01l\x027\x01t\x02user\x01h\x02tb_top.u_dut.SOME_COVER"
    raw = _write_dat(tmp_path, [f"C '{blob}' 2\n"])

    records = _make_cov().parse_user_cover_records(raw)

    assert records[0]["name"] == "SOME_COVER"


def test_missing_database_and_empty_database_return_none(tmp_path):
    cov = _make_cov()
    assert cov.parse_user_cover_records(str(tmp_path / "absent.dat")) is None
    assert cov.parse_user_cover_records(_write_dat(tmp_path, [])) is None


def test_functional_ratio_still_derives_from_the_same_records(tmp_path):
    raw = _write_dat(
        tmp_path,
        [
            _record(name="HIT_A", count=3),
            _record(name="HIT_B", count=1, line=90),
            _record(name="MISS", count=0, line=91),
            _record(name="ALSO_MISS", count=0, line=92),
        ],
    )

    assert _make_cov()._parse_raw_user_metric(raw) == 0.5


def test_aggregate_sums_hits_across_instances_of_one_source_point():
    records = [
        {"name": "C1", "file": "tb.sv", "line": 10, "hier": "top.a.C1", "hits": 3},
        {"name": "C1", "file": "tb.sv", "line": 10, "hier": "top.b.C1", "hits": 4},
    ]

    assert aggregate_cover_records(records) == [
        {"name": "C1", "file": "tb.sv", "line": 10, "hits": 7}
    ]


def test_aggregate_keeps_distinct_points_on_the_same_line_apart():
    records = [
        {"name": "C1", "file": "tb.sv", "line": 10, "hits": 1},
        {"name": "C2", "file": "tb.sv", "line": 10, "hits": 0},
    ]

    assert [e["name"] for e in aggregate_cover_records(records)] == ["C1", "C2"]


def test_aggregate_sorts_deterministically_and_tolerates_missing_fields():
    records = [
        {"name": "Z", "file": "b.sv", "line": 2, "hits": 1},
        {"name": "A", "file": "a.sv", "line": 30, "hits": 1},
        {"name": None, "file": None, "line": None, "hits": 5},
    ]

    assert [(e["file"], e["line"]) for e in aggregate_cover_records(records)] == [
        (None, None),
        ("a.sv", 30),
        ("b.sv", 2),
    ]


def test_aggregate_of_nothing_is_none():
    assert aggregate_cover_records([]) is None
    assert aggregate_cover_records(None) is None
