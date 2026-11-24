"""Tests for `activity.instance-map`: reading the pairs file and renaming a SAIF's instances."""

import hashlib
from textwrap import dedent

import pytest

from rtl_buddy.tools.saif_names import (
    InstanceMapError,
    apply_instance_map,
    read_instance_map,
)
from saif_support import OPENROAD, read_saif

# A trace of the netlist renamed for a simulator: `u_a.u0` is `u_a__u0` there.
_RENAMED_SAIF = dedent("""\
    (SAIFILE
      (SAIFVERSION "2.0")
      (DIRECTION "backward")
      (DESIGN)
      (DIVIDER /)
      (TIMESCALE 1 ps)
      (DURATION 2000)
      (INSTANCE top
        (NET
          (clk (T0 1000) (T1 1000) (TX 0) (TZ 0) (TC 3) (IG 0))
        )
        (INSTANCE u_a__u0
          (NET
            (A (T0 1000) (T1 1000) (TX 0) (TZ 0) (TC 3) (IG 0))
            (Y (T0 1000) (T1 1000) (TX 0) (TZ 0) (TC 2) (IG 0))
          )
        )
        (INSTANCE u1
          (NET
            (A (T0 1000) (T1 1000) (TX 0) (TZ 0) (TC 2) (IG 0))
          )
        )
      )
    )
    """)


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_read_instance_map_reads_pairs_and_skips_comments(tmp_path):
    path = _write(
        tmp_path,
        "map.txt",
        "# trace  database\n\nu_a__u0  \\u_a.u0\nmem_3_   u_a.mem\\[3\\]\n",
    )

    imap = read_instance_map(str(path))

    assert imap.pairs == {"u_a__u0": "u_a.u0", "mem_3_": "u_a.mem[3]"}
    assert imap.written == {"u_a__u0": "u_a.u0", "mem_3_": "u_a.mem\\[3\\]"}
    assert imap.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "text,match",
    [
        ("u_a__u0\n", "line 1: expected '<trace name> <database name>'"),
        ("a b c\n", "line 1: expected"),
        ("# c\nu0 u_a/u0\n", "line 2: read_saif cannot read the instance name"),
        ("u0 x\nu0 y\n", "line 2: 'u0' is mapped to both 'x' and 'y'"),
    ],
    ids=["one-name", "three-names", "unreadable-database-name", "two-targets"],
)
def test_read_instance_map_rejects_a_bad_line(tmp_path, text, match):
    path = _write(tmp_path, "map.txt", text)
    with pytest.raises(InstanceMapError, match=match):
        read_instance_map(str(path))


def test_read_instance_map_names_a_missing_file(tmp_path):
    with pytest.raises(InstanceMapError, match="cannot read activity.instance-map"):
        read_instance_map(str(tmp_path / "nope.txt"))


def test_a_repeated_identical_pair_is_accepted(tmp_path):
    path = _write(tmp_path, "map.txt", "u0 x\nu0 x\n")
    assert read_instance_map(str(path)).pairs == {"u0": "x"}


def test_apply_instance_map_renames_only_the_mapped_instances(tmp_path):
    saif = _write(tmp_path, "trace.saif", _RENAMED_SAIF)
    imap = read_instance_map(
        str(_write(tmp_path, "map.txt", "u_a__u0 u_a.u0\nu1 u9\nnot_there x\n"))
    )
    dest = tmp_path / "mapped.saif"

    result = apply_instance_map(str(saif), str(dest), imap)

    expected = _RENAMED_SAIF.replace("(INSTANCE u_a__u0", "(INSTANCE u_a.u0").replace(
        "(INSTANCE u1", "(INSTANCE u9"
    )
    assert dest.read_text() == expected
    assert result.renamed == 2
    assert result.unmatched == 1
    assert result.source_sha256 == hashlib.sha256(saif.read_bytes()).hexdigest()


def test_apply_instance_map_matches_an_escaped_spelling(tmp_path):
    """A SAIF from another tool may escape the trace name or give the design name in
    quotes; it still matches.
    """
    saif = _write(
        tmp_path,
        "trace.saif",
        '(SAIFILE\n  (INSTANCE top\n    (INSTANCE "cell" u\\.a\n    )\n  )\n)\n',
    )
    imap = read_instance_map(str(_write(tmp_path, "map.txt", "u.a u_a.mem[0]\n")))
    dest = tmp_path / "mapped.saif"

    result = apply_instance_map(str(saif), str(dest), imap)

    assert '(INSTANCE "cell" u_a.mem\\[0\\]\n' in dest.read_text()
    assert result.renamed == 1


@pytest.mark.skipif(OPENROAD is None, reason="openroad not installed")
def test_openroad_reads_the_mapped_instance_into_the_database(tmp_path):
    """Unmapped, the renamed instance's pins stay unannotated; mapped, they are."""
    saif = _write(tmp_path, "trace.saif", _RENAMED_SAIF)
    imap = read_instance_map(str(_write(tmp_path, "map.txt", "u_a__u0 u_a.u0\n")))
    mapped = tmp_path / "mapped.saif"
    apply_instance_map(str(saif), str(mapped), imap)

    before = read_saif(tmp_path, saif)
    after = read_saif(tmp_path, mapped)

    assert "Annotated 2 pin activities." in before, before
    assert " saif u_a.u0/A\n" not in before
    assert "Annotated 4 pin activities." in after, after
    for pin in ("u_a.u0/A", "u_a.u0/Y", "u1/A"):
        assert f" saif {pin}\n" in after, after


def test_apply_instance_map_reads_a_gzip_saif(tmp_path):
    """`read_saif` reads a gzip-compressed SAIF, so the map does too; the hash is of the
    compressed file as it is on disk.
    """
    import gzip

    saif = tmp_path / "trace.saif.gz"
    saif.write_bytes(gzip.compress(_RENAMED_SAIF.encode()))
    imap = read_instance_map(str(_write(tmp_path, "map.txt", "u_a__u0 u_a.u0\n")))
    dest = tmp_path / "mapped.saif"

    result = apply_instance_map(str(saif), str(dest), imap)

    assert dest.read_text() == _RENAMED_SAIF.replace("u_a__u0", "u_a.u0")
    assert result.renamed == 1
    assert result.source_sha256 == hashlib.sha256(saif.read_bytes()).hexdigest()
