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
    col=17,
    name="APB_IF_WRITE",
    page="v_user/tb_top",
    hier=None,
    count=13,
    type_="user",
):
    """Build one raw `C '...' <count>` record in Verilator's key encoding.

    Key set and ordering match what Verilator 5.049 actually writes, `n`
    (column) included — the shape verified against a real `coverage.dat`.
    """
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


def test_parses_name_file_line_and_hits(tmp_path):
    raw = _write_dat(tmp_path, [_record()])

    records = _make_cov().parse_user_cover_records(raw)

    assert records == [
        {
            "name": "APB_IF_WRITE",
            "file": "../../tb_top.sv",
            "line": 89,
            "module": "tb_top",
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


def test_aggregate_sums_hits_for_one_point_across_tests():
    """The cross-test fold: the same point seen in two per-test databases.

    Within a single database Verilator has already merged a point's instances
    (see the verbatim-database tests below); this fold is what combines one
    test's counts with another's.
    """
    records = [
        {"name": "C1", "file": "tb.sv", "line": 10, "module": "m", "hits": 3},
        {"name": "C1", "file": "tb.sv", "line": 10, "module": "m", "hits": 4},
    ]

    assert aggregate_cover_records(records) == [
        {"name": "C1", "file": "tb.sv", "line": 10, "module": "m", "hits": 7}
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


# --- Verbatim databases -------------------------------------------------
#
# The records below are copied byte-for-byte out of `coverage.dat` files
# written by Verilator 5.049 for a real `cover property` design, rather than
# reconstructed from the format description. They pin behaviour that only
# shows up against the real writer.

_REAL_DAT = (
    "# SystemC::Coverage-3\n"
    "C '\x01f\x02tb_top.sv\x01l\x0214\x01n\x0217\x01t\x02user\x01page\x02v_user/tb_top"
    "\x01o\x02APB_IF_WRITE\x01h\x02tb_top.APB_IF_WRITE' 3\n"
    "C '\x01f\x02tb_top.sv\x01l\x0215\x01n\x0217\x01t\x02user\x01page\x02v_user/tb_top"
    "\x01o\x02APB_IF_READ\x01h\x02tb_top.APB_IF_READ' 2\n"
    "C '\x01f\x02tb_top.sv\x01l\x0216\x01n\x0217\x01t\x02user\x01page\x02v_user/tb_top"
    "\x01o\x02NEVER_HIT\x01h\x02tb_top.NEVER_HIT' 0\n"
    # Two `sub` instances: Verilator merged them into one record, wildcarding
    # the differing hierarchy component and summing the counts (3 + 2).
    "C '\x01f\x02tb_top.sv\x01l\x022\x01n\x0214\x01t\x02user\x01page\x02v_user/sub"
    "\x01o\x02SUB_COVER\x01h\x02tb_top.u*.SUB_COVER' 5\n"
)

# Same cover property `include`d into two modules. Verilator keys these apart
# by `page`, so identical file/line/name arrive as two records.
_REAL_SHARED_INCLUDE_DAT = (
    "# SystemC::Coverage-3\n"
    "C '\x01f\x02chk.svh\x01l\x021\x01n\x0213\x01t\x02user\x01page\x02v_user/modA"
    "\x01o\x02SHARED_CHK\x01h\x02tb_top.a.SHARED_CHK' 3\n"
    "C '\x01f\x02chk.svh\x01l\x021\x01n\x0213\x01t\x02user\x01page\x02v_user/modB"
    "\x01o\x02SHARED_CHK\x01h\x02tb_top.b.SHARED_CHK' 2\n"
)


def _write_raw(tmp_path, text, name="coverage.dat"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_real_database_parses_every_point(tmp_path):
    records = _make_cov().parse_user_cover_records(_write_raw(tmp_path, _REAL_DAT))

    assert records == [
        {
            "name": "APB_IF_WRITE",
            "file": "tb_top.sv",
            "line": 14,
            "module": "tb_top",
            "hier": "tb_top.APB_IF_WRITE",
            "hits": 3,
        },
        {
            "name": "APB_IF_READ",
            "file": "tb_top.sv",
            "line": 15,
            "module": "tb_top",
            "hier": "tb_top.APB_IF_READ",
            "hits": 2,
        },
        {
            "name": "NEVER_HIT",
            "file": "tb_top.sv",
            "line": 16,
            "module": "tb_top",
            "hier": "tb_top.NEVER_HIT",
            "hits": 0,
        },
        {
            "name": "SUB_COVER",
            "file": "tb_top.sv",
            "line": 2,
            "module": "sub",
            "hier": "tb_top.u*.SUB_COVER",
            "hits": 5,
        },
    ]


def test_real_database_list_matches_the_functional_denominator(tmp_path):
    """Verilator pre-merges instances, so the two views agree point-for-point."""
    cov = _make_cov()
    raw = _write_raw(tmp_path, _REAL_DAT)

    covers = aggregate_cover_records(cov.parse_user_cover_records(raw))
    hit = sum(1 for c in covers if c["hits"] > 0)

    assert len(covers) == 4
    assert hit / len(covers) == cov._parse_raw_user_metric(raw) == 0.75


def test_cross_test_fold_matches_verilator_coverage_merge(tmp_path):
    """Folding two real per-test databases reproduces `verilator_coverage --write`.

    The expected counts are what the tool itself produced when merging these
    two databases: 3+1, 2+0, 0+0, 5+1.
    """
    run_b = (
        _REAL_DAT.replace("APB_IF_WRITE' 3", "APB_IF_WRITE' 1")
        .replace("APB_IF_READ' 2", "APB_IF_READ' 0")
        .replace("SUB_COVER' 5", "SUB_COVER' 1")
    )

    cov = _make_cov()
    records = []
    for i, text in enumerate((_REAL_DAT, run_b)):
        records.extend(
            cov.parse_user_cover_records(_write_raw(tmp_path, text, f"run{i}.dat"))
        )

    assert aggregate_cover_records(records) == [
        {
            "name": "SUB_COVER",
            "file": "tb_top.sv",
            "line": 2,
            "module": "sub",
            "hits": 6,
        },
        {
            "name": "APB_IF_WRITE",
            "file": "tb_top.sv",
            "line": 14,
            "module": "tb_top",
            "hits": 4,
        },
        {
            "name": "APB_IF_READ",
            "file": "tb_top.sv",
            "line": 15,
            "module": "tb_top",
            "hits": 2,
        },
        {
            "name": "NEVER_HIT",
            "file": "tb_top.sv",
            "line": 16,
            "module": "tb_top",
            "hits": 0,
        },
    ]


def test_shared_include_stays_split_per_module(tmp_path):
    """One label compiled into two modules stays two entries, one per module.

    Verilator keeps these apart by `page` and so does the fold, because
    combining them would hide "hit in modA, never in modB" behind a single
    nonzero count — information a consumer cannot recover. Folding by `name`
    is something a consumer can do itself.
    """
    cov = _make_cov()
    raw = _write_raw(tmp_path, _REAL_SHARED_INCLUDE_DAT)

    covers = aggregate_cover_records(cov.parse_user_cover_records(raw))

    assert covers == [
        {
            "name": "SHARED_CHK",
            "file": "chk.svh",
            "line": 1,
            "module": "modA",
            "hits": 3,
        },
        {
            "name": "SHARED_CHK",
            "file": "chk.svh",
            "line": 1,
            "module": "modB",
            "hits": 2,
        },
    ]


def test_one_module_covered_and_another_not_stays_visible(tmp_path):
    """The case the split exists for: a per-module hole must not read as covered."""
    cov = _make_cov()
    raw = _write_raw(
        tmp_path, _REAL_SHARED_INCLUDE_DAT.replace("SHARED_CHK' 2", "SHARED_CHK' 0")
    )

    covers = aggregate_cover_records(cov.parse_user_cover_records(raw))

    assert [(c["module"], c["hits"]) for c in covers] == [("modA", 3), ("modB", 0)]
    # And a consumer folding by label to union semantics still can.
    assert sum(c["hits"] for c in covers if c["name"] == "SHARED_CHK") == 3


def test_covers_length_matches_functional_denominator_in_every_case(tmp_path):
    """With `module` in the key the two views agree — no documented exception."""
    cov = _make_cov()
    for text in (_REAL_DAT, _REAL_SHARED_INCLUDE_DAT):
        raw = _write_raw(tmp_path, text, name=f"{hash(text) & 0xFFFF}.dat")
        records = cov.parse_user_cover_records(raw)
        covers = aggregate_cover_records(records)
        hit = sum(1 for c in covers if c["hits"] > 0)

        assert len(covers) == len(records)
        assert hit / len(covers) == cov._parse_raw_user_metric(raw)
