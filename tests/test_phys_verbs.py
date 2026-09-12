"""
Tests for #558 phase 2 — the `rb phys` verbs and the artefacts they read.

What these pin:

* `rb phys summary` / `module` / `instance` answer from a run's
  `phys-manifest.json` and the model it names, and run nothing;
* their `--machine` payloads are exactly the dicts the payload builders
  return, since a later MCP tool wraps them verbatim;
* discovery precedence: an explicit `--manifest` beats `--phys-dir`, which
  beats the newest manifest under the project root;
* a model with only one half says which half is missing and which command
  would produce it, rather than reporting zeros;
* an unknown module or instance exits 2 with near misses, and a project
  with no artefacts at all exits 2 naming the commands that write them.

Console assertions read `result.output` rather than caplog: the CLI's own
events do not reach a caplog handler.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.phys.manifest import MANIFEST_FILENAME, build_manifest, write_manifest
from rtl_buddy.phys.model import (
    build_power_model,
    build_synth_model,
    merge_model,
    write_model,
)
from rtl_buddy.rtl_buddy import RtlBuddy

_FIXTURES = Path(__file__).parent / "fixtures"

_MODULES = [
    {"module": "blk", "cell_count": 120, "area_um2": 480.5},
    {"module": "sub", "cell_count": 40, "area_um2": 96.0},
]

_INSTANCES = [
    {
        "instance_path": "u_sub/_64_",
        "module": "sub",
        "leakage_uw": 0.079,
        "internal_uw": 2.28,
        "switching_uw": 0.0675,
        "total_uw": 2.42,
    },
    {
        "instance_path": "u_sub/u_leaf/_12_",
        "module": "sub",
        "leakage_uw": 0.001,
        "internal_uw": 0.5,
        "switching_uw": 0.25,
        "total_uw": 0.751,
    },
]


def _write_run(root: Path, run: str, *, modules=None, instances=None, mtime=None):
    """One run's artefacts, written by the phase-1 producers."""
    phys_dir = root / "verif" / "blk" / "artefacts" / run
    phys_dir.mkdir(parents=True, exist_ok=True)

    model = None
    if modules is not None:
        model = build_synth_model(
            top="blk", modules=modules, area_um2=576.5, gate_count=160
        )
    if instances is not None:
        power = build_power_model(
            top="blk",
            instances=instances,
            internal_w=2.78e-6,
            switching_w=0.3175e-6,
            leakage_w=0.08e-6,
            total_w=3.171e-6,
        )
        model = merge_model(model, power) if model is not None else power
    model_path = write_model(model, phys_dir)

    manifest_path = write_manifest(
        build_manifest(
            project_root=root,
            phys_dir=phys_dir,
            command="power" if instances is not None else "synth",
            run=run,
            top="blk",
            model_path=model_path,
            totals=model["totals"],
            synth=(
                None
                if modules is None
                else {
                    "backend": "yosys",
                    "run": run,
                    "stats": phys_dir / "synth_stat.json",
                    "netlist": phys_dir / "synth_netlist.v",
                    "log": phys_dir / "synth.log",
                }
            ),
            power=(
                None
                if instances is None
                else {
                    "backend": "openroad",
                    "run": run,
                    "netlist_source": "synth",
                    "report": phys_dir / "power.rpt",
                    "instances": phys_dir / "power_instances.rpt",
                    "cells": phys_dir / "power_instances.cells",
                    "log": phys_dir / "power.log",
                }
            ),
        ),
        phys_dir,
    )
    if mtime is not None:
        os.utime(manifest_path, (mtime, mtime))
    return phys_dir


_LIVE: list[RtlBuddy] = []


def _runner() -> tuple[CliRunner, RtlBuddy]:
    """A fresh CLI object, with the previous one's artefact lock released."""
    while _LIVE:
        _LIVE.pop()._artifact_locks.release_all()
    rb = RtlBuddy(name="test_phys_verbs")
    _LIVE.append(rb)
    return CliRunner(), rb


@pytest.fixture
def phys_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runnable project root with two runs' physical artefacts."""
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copy(_FIXTURES / "minimal_project" / "root_config.yaml", root)
    _write_run(root, "old_synth", modules=_MODULES, mtime=1_000_000)
    _write_run(root, "both", modules=_MODULES, instances=_INSTANCES, mtime=2_000_000)
    monkeypatch.chdir(root)
    return root


def _machine(result) -> dict:
    assert result.exit_code in (0, 1, 2), result.output
    return json.loads(result.output.strip().splitlines()[-1])


# --- summary ----------------------------------------------------------------


def test_phys_summary_reads_the_newest_manifest(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "summary"])

    envelope = _machine(result)
    assert envelope["command"] == "phys summary"
    assert envelope["exit_code"] == 0
    payload = envelope["payload"]
    assert payload["run"] == "both"
    assert payload["backends"] == {"synth": "yosys", "power": "openroad"}
    assert payload["counts"] == {"modules": 2, "instances": 2}
    assert payload["totals"]["cell_count"] == 160
    assert [row["module"] for row in payload["modules"]] == ["blk", "sub"]
    assert [row["instance_path"] for row in payload["instances"]] == [
        "u_sub/_64_",
        "u_sub/u_leaf/_12_",
    ]
    assert payload["artefacts"]["manifest"] == (
        "verif/blk/artefacts/both/phys-manifest.json"
    )


def test_phys_summary_renders_without_machine_mode(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "summary"])

    assert result.exit_code == 0, result.output
    assert "verif/blk/artefacts/both/phys-manifest.json" in result.output
    assert "u_sub/_64_" in result.output


def test_phys_summary_says_which_half_is_missing(phys_project):
    runner, rb = _runner()

    result = runner.invoke(
        rb.app, ["phys", "summary", "--phys-dir", "verif/blk/artefacts/old_synth"]
    )

    assert result.exit_code == 0, result.output
    assert "no per-instance rows in this model" in result.output
    assert "rb power" in result.output


def test_phys_summary_limit_truncates_the_rankings(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "summary", "--limit", "1"])

    payload = _machine(result)["payload"]
    assert len(payload["modules"]) == 1
    assert len(payload["instances"]) == 1


# --- discovery precedence ---------------------------------------------------


def test_explicit_phys_dir_beats_discovery(phys_project):
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        [
            "--machine",
            "phys",
            "summary",
            "--phys-dir",
            "verif/blk/artefacts/old_synth",
        ],
    )

    payload = _machine(result)["payload"]
    assert payload["run"] == "old_synth"
    assert payload["missing_halves"] == ["instances"]


def test_explicit_manifest_beats_phys_dir(phys_project):
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        [
            "--machine",
            "phys",
            "summary",
            "--phys-dir",
            "verif/blk/artefacts/old_synth",
            "--manifest",
            f"verif/blk/artefacts/both/{MANIFEST_FILENAME}",
        ],
    )

    assert _machine(result)["payload"]["run"] == "both"


def test_phys_verbs_fail_loudly_with_no_artefacts(tmp_path, monkeypatch):
    shutil.copy(_FIXTURES / "minimal_project" / "root_config.yaml", tmp_path)
    monkeypatch.chdir(tmp_path)
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "summary"])

    envelope = _machine(result)
    assert envelope["exit_code"] == 2
    error = envelope["payload"]["error"]
    assert MANIFEST_FILENAME in error
    assert "rb synth" in error and "rb power" in error


# --- module -----------------------------------------------------------------


def test_phys_module_joins_cells_area_and_instance_power(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "module", "sub"])

    payload = _machine(result)["payload"]
    assert payload["row"] == {"module": "sub", "cell_count": 40, "area_um2": 96.0}
    assert payload["instance_count"] == 2
    assert payload["power"]["total_uw"] == pytest.approx(3.171)


def test_phys_module_renders_its_instances(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "module", "sub"])

    assert result.exit_code == 0, result.output
    assert "instances of sub: 2/2" in result.output
    # The table ellipsizes a path too long for the column, as the coverage
    # tables do; the prefix is what a reader matches on.
    assert "u_sub/u_leaf" in result.output


def test_phys_module_unknown_name_exits_two_with_candidates(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "module", "subb"])

    envelope = _machine(result)
    assert envelope["exit_code"] == 2
    assert "sub" in envelope["payload"]["candidates"]


def test_phys_module_prints_candidates_without_machine_mode(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "module", "subb"])

    assert result.exit_code == 2
    assert "did you mean" in result.output


# --- instance ---------------------------------------------------------------


def test_phys_instance_answers_an_exact_leaf(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "instance", "u_sub/_64_"])

    payload = _machine(result)["payload"]
    assert payload["match"] == "exact"
    assert payload["children"] == []
    assert payload["rollup"]["instances"] == 1
    assert payload["rollup"]["total_uw"] == pytest.approx(2.42)


def test_phys_instance_rolls_up_a_subtree(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "instance", "u_sub"])

    payload = _machine(result)["payload"]
    assert payload["match"] == "prefix"
    assert [row["instance_path"] for row in payload["children"]] == [
        "u_sub/_64_",
        "u_sub/u_leaf/_12_",
    ]
    assert payload["rollup"]["instances"] == 2
    assert payload["rollup"]["total_uw"] == pytest.approx(3.171)
    assert payload["rollup"]["area_um2"] == pytest.approx(192.0)


def test_phys_instance_renders_the_rollup(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "instance", "u_sub"])

    assert result.exit_code == 0, result.output
    assert "prefix match, 2 leaf instance(s)" in result.output
    assert "rollup (2)" in result.output
    assert "subtree area:" in result.output


def test_phys_instance_unknown_path_exits_two_with_candidates(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "instance", "u_sub/_65_"])

    envelope = _machine(result)
    assert envelope["exit_code"] == 2
    assert "u_sub/_64_" in envelope["payload"]["candidates"]


def test_phys_instance_on_a_synth_only_model_names_the_power_command(phys_project):
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        [
            "--machine",
            "phys",
            "instance",
            "u_sub",
            "--phys-dir",
            "verif/blk/artefacts/old_synth",
        ],
    )

    envelope = _machine(result)
    assert envelope["exit_code"] == 2
    assert "rb power" in envelope["payload"]["error"]
