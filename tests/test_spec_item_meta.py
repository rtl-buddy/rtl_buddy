"""``meta:`` on a coverage item reaches ``SpecCoverageItem`` and ``rb spec check-coverage --machine``; other unknown item keys warn."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from typer.testing import CliRunner

from rtl_buddy.config.spec import SpecConfig
from rtl_buddy.rtl_buddy import RtlBuddy

_SPEC = """\
rtl-buddy-filetype: spec_config
blocks:
  - name: my_block
    desc: example
    coverage-items:
      - id: MYBLK-COV-01
        desc: normal operation
        meta: {tier: pr, owner: alice}
      - id: MYBLK-COV-02
        desc: error recovery
"""

_SPEC_UNKNOWN = _SPEC.replace(
    "        desc: error recovery\n",
    "        desc: error recovery\n        tier: pr\n        level: 2\n",
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "spec" / "specs.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    return path


def _items(path: Path):
    return SpecConfig(str(path)).get_blocks()[0].coverage_items


def _warnings(caplog):
    return [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "spec_config.unknown_key"
    ]


def test_meta_is_carried_to_the_item(tmp_path):
    first, second = _items(_write(tmp_path, _SPEC))
    assert first.meta == {"tier": "pr", "owner": "alice"}
    assert second.meta == {}


def test_meta_is_in_machine_output(tmp_path):
    _write(tmp_path, _SPEC)
    (tmp_path / "verif").mkdir()
    result = CliRunner().invoke(
        RtlBuddy(name="test_spec_item_meta").app,
        [
            "--machine",
            "spec",
            "check-coverage",
            "--spec-dir",
            str(tmp_path / "spec"),
            "--verif-dir",
            str(tmp_path / "verif"),
        ],
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    items = {i["id"]: i for i in json.loads(lines[-1])["payload"]["items"]}
    assert items["MYBLK-COV-01"]["meta"] == {"tier": "pr", "owner": "alice"}
    assert "meta" not in items["MYBLK-COV-02"]


def test_machine_output_carries_dates_and_non_finite_floats_as_strings(tmp_path):
    _write(
        tmp_path,
        _SPEC.replace(
            "meta: {tier: pr, owner: alice}",
            "meta: {due: 2026-01-15, at: 2026-01-15 10:00:00, cap: .inf, gap: .nan}",
        ),
    )
    (tmp_path / "verif").mkdir()
    result = CliRunner().invoke(
        RtlBuddy(name="test_spec_item_meta_dates").app,
        [
            "--machine",
            "spec",
            "check-coverage",
            "--spec-dir",
            str(tmp_path / "spec"),
            "--verif-dir",
            str(tmp_path / "verif"),
        ],
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    item = json.loads(lines[-1])["payload"]["items"][0]
    assert item["meta"] == {
        "due": "2026-01-15",
        "at": "2026-01-15T10:00:00",
        "cap": "inf",
        "gap": "nan",
    }


def test_null_meta_is_empty(tmp_path):
    for empty in ("meta:", "meta: ~"):
        text = _SPEC.replace("meta: {tier: pr, owner: alice}", empty)
        assert _items(_write(tmp_path, text))[0].meta == {}


def test_meta_does_not_warn(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        _items(_write(tmp_path, _SPEC))
    assert not _warnings(caplog)


def test_unknown_item_key_warns_and_loads(tmp_path, caplog):
    path = _write(tmp_path, _SPEC_UNKNOWN)
    with caplog.at_level(logging.WARNING):
        items = _items(path)
    assert [i.id for i in items] == ["MYBLK-COV-01", "MYBLK-COV-02"]
    warned = _warnings(caplog)
    assert {r.rtl_fields["key"] for r in warned} == {"tier", "level"}
    for record in warned:
        assert record.levelno == logging.WARNING
        assert "MYBLK-COV-02" in record.rtl_fields["where"]
        message = record.getMessage()
        assert str(path) in message
        assert "MYBLK-COV-02" in message
        assert "meta:" in message


def test_unknown_item_key_warns_once_per_process(tmp_path, caplog):
    path = _write(tmp_path, _SPEC_UNKNOWN)
    _items(path)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _items(path)
    assert not _warnings(caplog)


def test_unknown_block_and_top_level_keys_warn(tmp_path, caplog):
    text = _SPEC.replace(
        "    desc: example\n", "    desc: example\n    meta: {tier: pr}\n", 1
    ).replace("blocks:\n", "owner: alice\nblocks:\n", 1)
    path = _write(tmp_path, text)
    with caplog.at_level(logging.WARNING):
        SpecConfig(str(path))
    warned = {(r.rtl_fields["where"], r.rtl_fields["key"]) for r in _warnings(caplog)}
    assert warned == {
        ("top level", "owner"),
        ("block 'my_block'", "meta"),
    }
