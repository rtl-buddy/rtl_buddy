"""Tests for the rb xplr P0 contract — record schema + ledger layout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.exec_context import ExecutionContext
from rtl_buddy.xplr import ledger
from rtl_buddy.xplr.schema import (
    ABSENT,
    SCHEMA_VERSION,
    ExperimentRecord,
    dumps_record,
    loads_record,
    schema,
    validate_record,
)


FIXTURES = Path(__file__).parent / "fixtures" / "xplr"
VALID_FIXTURES = sorted((FIXTURES / "valid").glob("*.json"))
INVALID_FIXTURES = sorted((FIXTURES / "invalid").glob("*.json"))


def _fixture_ids(paths: list[Path]) -> list[str]:
    return [p.stem for p in paths]


# ---------------------------------------------------------------------------
# schema basics
# ---------------------------------------------------------------------------


def test_schema_is_loadable_and_versioned():
    s = schema()
    assert s["$id"].endswith(f"xplr-experiment-{SCHEMA_VERSION}.json")
    assert s["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    # strictness is part of the contract
    assert s["additionalProperties"] is False


def test_fixture_inventory_matches_contract():
    # The two #296 samples, one minimal record, one mockflow-flavored record.
    assert _fixture_ids(VALID_FIXTURES) == [
        "exp-0001",
        "exp-0002",
        "exp-0007",
        "exp-0008",
    ]
    assert len(INVALID_FIXTURES) >= 3


# ---------------------------------------------------------------------------
# round trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=_fixture_ids(VALID_FIXTURES))
def test_valid_fixture_round_trips_byte_identically(path: Path):
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)

    validate_record(data)  # raw dict passes the schema
    record = loads_record(text)  # typed view
    assert isinstance(record, ExperimentRecord)
    assert record.id == path.stem
    assert record.schema_version == SCHEMA_VERSION

    assert record.to_dict() == data
    assert dumps_record(record) == text  # byte-identical serialization


def test_sample_a_typed_accessors():
    record = loads_record((FIXTURES / "valid" / "exp-0007.json").read_text())
    assert record.parent == "exp-0003"
    assert record.source.git_sha == "9f3a1c4"
    assert record.source.diff_from == "1b0e7a2"
    assert record.source.dirty is False
    assert [k.name for k in record.knobs] == [
        "partition.blk_c.fpga",
        "vivado.blk_c.pblock",
        "vivado.place.directive",
    ]
    # knob values are untyped; null from-values survive
    assert record.knobs[1].from_ is None
    assert record.knobs[1].to == "SLR1"
    assert record.knobs[0].layer == "flow"
    assert record.config_snapshot["vivado"]["seed"] == 3
    assert record.outcome.status == "success"
    assert record.outcome.metrics["routed"] is True
    assert record.outcome.metric_meta["wns_ns_fb2"].direction == "max"
    assert record.provenance.reused_state == "sg0"
    assert [t.name for t in record.provenance.tools] == ["protocompiler", "vivado"]


def test_minimal_record_has_absent_optionals():
    record = loads_record((FIXTURES / "valid" / "exp-0001.json").read_text())
    assert record.parent is ABSENT
    assert record.hypothesis is ABSENT
    assert record.config_snapshot is ABSENT
    assert record.knobs == []
    assert record.source.branch is ABSENT
    assert record.source.dirty is ABSENT
    assert record.outcome.metrics is ABSENT
    assert record.provenance.tools is ABSENT


def test_explicit_null_parent_is_preserved():
    data = json.loads((FIXTURES / "valid" / "exp-0001.json").read_text())
    data["parent"] = None
    record = ExperimentRecord.from_dict(data)
    assert record.parent is None
    assert record.to_dict() == data
    assert "parent" in record.to_dict()


# ---------------------------------------------------------------------------
# invalid records
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", INVALID_FIXTURES, ids=_fixture_ids(INVALID_FIXTURES))
def test_invalid_fixture_is_rejected(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(FatalRtlBuddyError):
        validate_record(data)
    with pytest.raises(FatalRtlBuddyError):
        ExperimentRecord.from_dict(data)


@pytest.mark.parametrize(
    ("name", "pointer", "fragment"),
    [
        ("bad-sha.json", "/source/git_sha", "not-a-sha"),
        ("unknown-field.json", "/", "notes"),
        ("bad-status.json", "/outcome/status", "done"),
        ("bad-created.json", "/provenance/created", "date-time"),
    ],
)
def test_invalid_fixture_message_is_useful(name: str, pointer: str, fragment: str):
    data = json.loads((FIXTURES / "invalid" / name).read_text())
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        validate_record(data)
    message = str(excinfo.value)
    assert pointer in message
    assert fragment in message


def test_date_time_requires_timezone_offset():
    data = json.loads((FIXTURES / "valid" / "exp-0001.json").read_text())
    data["provenance"]["created"] = "2026-06-10T09:00:00"  # naive timestamp
    with pytest.raises(FatalRtlBuddyError, match="date-time"):
        validate_record(data)


def test_validate_record_rejects_non_object():
    with pytest.raises(FatalRtlBuddyError, match="JSON object"):
        validate_record(["not", "an", "object"])


def test_loads_record_rejects_malformed_json():
    with pytest.raises(FatalRtlBuddyError, match="not valid JSON"):
        loads_record("{ this is not json")


# ---------------------------------------------------------------------------
# ledger layout
# ---------------------------------------------------------------------------


def _load_fixture_record(name: str) -> ExperimentRecord:
    return loads_record((FIXTURES / "valid" / name).read_text(encoding="utf-8"))


def test_ledger_root_follows_artifact_dir_convention(tmp_path: Path):
    ctx = ExecutionContext.for_dir(tmp_path, tmp_path)
    assert ledger.ledger_root(ctx) == tmp_path / "artefacts" / "xplr"


def test_next_id_starts_at_one(tmp_path: Path):
    assert ledger.next_id(tmp_path / "does-not-exist") == "exp-0001"
    assert ledger.next_id(tmp_path) == "exp-0001"


def test_next_id_skips_past_existing_dirs_even_without_record(tmp_path: Path):
    (tmp_path / "exp-0007").mkdir()  # crashed run: dir but no record.json
    (tmp_path / "exp-0003").mkdir()
    (tmp_path / "my-named-exp").mkdir()  # non-auto ids don't participate
    assert ledger.next_id(tmp_path) == "exp-0008"


def test_write_read_record_round_trip(tmp_path: Path):
    record = _load_fixture_record("exp-0007.json")
    path = ledger.write_record(tmp_path, record)
    assert path == tmp_path / "exp-0007" / "record.json"
    assert path.is_file()
    # written bytes are the canonical serialization
    assert path.read_text(encoding="utf-8") == dumps_record(record)
    # no temp files left behind
    assert sorted(p.name for p in path.parent.iterdir()) == ["record.json"]

    loaded = ledger.read_record(tmp_path, "exp-0007")
    assert loaded == record


def test_write_record_rejects_invalid_record(tmp_path: Path):
    record = _load_fixture_record("exp-0001.json")
    record.outcome.status = "done"  # mutate into an invalid state
    with pytest.raises(FatalRtlBuddyError, match="/outcome/status"):
        ledger.write_record(tmp_path, record)
    assert not (tmp_path / "exp-0001").exists()


def test_write_record_rejects_unsafe_id(tmp_path: Path):
    record = _load_fixture_record("exp-0001.json")
    record.id = "../escape"
    with pytest.raises(FatalRtlBuddyError, match="invalid experiment id"):
        ledger.write_record(tmp_path, record)


def test_read_record_missing_raises(tmp_path: Path):
    with pytest.raises(FatalRtlBuddyError, match="exp-9999"):
        ledger.read_record(tmp_path, "exp-9999")


def test_read_record_corrupt_json_names_the_file(tmp_path: Path):
    path = tmp_path / "exp-0001" / "record.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ nope", encoding="utf-8")
    with pytest.raises(FatalRtlBuddyError, match="record.json"):
        ledger.read_record(tmp_path, "exp-0001")


def test_list_records_sorted_and_skips_recordless_dirs(tmp_path: Path):
    for name in ["exp-0008.json", "exp-0001.json", "exp-0007.json"]:
        ledger.write_record(tmp_path, _load_fixture_record(name))
    (tmp_path / "exp-0005").mkdir()  # crashed run, no record.json
    (tmp_path / "stray-file.txt").write_text("not a record dir")

    records = ledger.list_records(tmp_path)
    assert [r.id for r in records] == ["exp-0001", "exp-0007", "exp-0008"]
    assert ledger.next_id(tmp_path) == "exp-0009"


def test_list_records_empty_root(tmp_path: Path):
    assert ledger.list_records(tmp_path / "missing") == []
