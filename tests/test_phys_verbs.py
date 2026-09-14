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
events do not reach a caplog handler. Every such assertion of more than
one word goes through `_flat()` — the console is Rich, Rich wraps at the
terminal width, and the width is a property of the machine the suite
runs on, so a sentence asserted verbatim passes on a wide terminal and
fails in CI with the break landing mid-phrase (#570).
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
from rtl_buddy.phys import query as phys_query_mod
from rtl_buddy.phys.provenance import activity_block, config_block
from rtl_buddy.phys.query import (
    INSTANCE_JOIN_LIBERTY_ONLY,
    INSTANCE_JOIN_NAME_COLLISION,
    PhysQueryError,
)
from rtl_buddy.rtl_buddy import RtlBuddy

_FIXTURES = Path(__file__).parent / "fixtures"

_MODULES = [
    {"module": "blk", "cell_count": 120, "area_um2": 480.5},
    {"module": "sub", "cell_count": 40, "area_um2": 96.0},
]

# The leaves name Liberty cells, the synthesis rows name RTL modules —
# two namespaces, kept apart here so `_COLLIDING_INSTANCES` below is the
# case it is meant to be.
_INSTANCES = [
    {
        "instance_path": "u_sub/_64_",
        "module": "DFF_X1",
        "leakage_uw": 0.079,
        "internal_uw": 2.28,
        "switching_uw": 0.0675,
        "total_uw": 2.42,
    },
    {
        "instance_path": "u_sub/u_leaf/_12_",
        "module": "DFF_X1",
        "leakage_uw": 0.001,
        "internal_uw": 0.5,
        "switching_uw": 0.25,
        "total_uw": 0.751,
    },
]


#: A design whose Liberty cell is named after one of its RTL modules.
_COLLIDING_INSTANCES = [{**row, "module": "sub"} for row in _INSTANCES]


#: Both halves of a fixture run record the same netlist hash, because
#: that is what a `rb synth` then `rb power` pair records and what the
#: merge requires before either half inherits the other (see
#: :func:`rtl_buddy.phys.model.may_inherit_other_half`).
_FIXTURE_NETLIST_SHA256 = "0" * 64


def _write_run(
    root: Path,
    run: str,
    *,
    modules=None,
    instances=None,
    mtime=None,
    netlist_sha256=_FIXTURE_NETLIST_SHA256,
    netlist_source="synth",
    mode=None,
    activity=None,
    synth_config=None,
    power_config=None,
):
    """One run's artefacts, written by the phase-1 producers."""
    phys_dir = root / "verif" / "blk" / "artefacts" / run
    phys_dir.mkdir(parents=True, exist_ok=True)

    model = None
    if modules is not None:
        model = build_synth_model(
            top="blk",
            modules=modules,
            area_um2=576.5,
            gate_count=160,
            netlist_sha256=netlist_sha256,
            config=synth_config,
        )
    if instances is not None:
        power = build_power_model(
            top="blk",
            instances=instances,
            internal_w=2.78e-6,
            switching_w=0.3175e-6,
            leakage_w=0.08e-6,
            total_w=3.171e-6,
            netlist_sha256=netlist_sha256,
            mode=mode,
            activity=activity,
            config=power_config,
        )
        model = (
            merge_model(model, power, own_half="instances")
            if model is not None
            else power
        )
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
                    "config": synth_config,
                }
            ),
            power=(
                None
                if instances is None
                else {
                    "backend": "openroad",
                    "run": run,
                    "netlist_source": netlist_source,
                    "report": phys_dir / "power.rpt",
                    "instances": phys_dir / "power_instances.rpt",
                    "cells": phys_dir / "power_instances.cells",
                    "log": phys_dir / "power.log",
                    "mode": mode,
                    "activity": activity,
                    "config": power_config,
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
    _write_run(root, "power_only", instances=_INSTANCES, mtime=1_500_000)
    # A power run off a routed database: no netlist to hash, so nothing a
    # later synthesis could pair its own netlist with.
    _write_run(
        root,
        "pnr_power",
        instances=_INSTANCES,
        mtime=1_600_000,
        netlist_sha256=None,
        netlist_source="pnr",
    )
    _write_run(
        root,
        "collision",
        modules=_MODULES,
        instances=_COLLIDING_INSTANCES,
        mtime=1_700_000,
    )
    _write_run(
        root,
        "both",
        modules=_MODULES,
        instances=_INSTANCES,
        mtime=2_000_000,
        mode="dynamic",
        activity=activity_block(
            source="saif", trace="verif/blk/artefacts/csr_smoke/dump.saif"
        ),
        synth_config=config_block(
            platform="nangate45", effort="timing-opt", options={"strategy": "TIMING"}
        ),
    )
    monkeypatch.chdir(root)
    return root


def _flat(output: str) -> str:
    """The console output with Rich's wrapping folded away, so a sentence
    longer than a line can still be asserted on as one.

    Not an optional convenience: Rich wraps to the *terminal's* width,
    which is the CI runner's and not the developer's, so `"rb synth" in
    result.output` is a test that passes locally and fails on a narrower
    console with the break between the two words (#570). Any assertion
    on more than one word of console text belongs here.
    """
    return " ".join(output.split())


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
    assert "no per-instance rows in this model" in _flat(result.output)
    assert "rb power" in _flat(result.output)


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


@pytest.mark.parametrize("document", [MANIFEST_FILENAME, "phys-model.json"])
def test_a_document_that_is_not_an_object_still_yields_an_error_envelope(
    phys_project, document
):
    """The finding (#561 review, Codex P2). A JSON root that is not an object
    used to raise `AttributeError` out of the query layer, so `--machine`
    emitted a traceback and no envelope — the one thing an agent surface
    cannot read. It is a refusal like any other now."""
    (phys_project / "verif" / "blk" / "artefacts" / "both" / document).write_text(
        "[]", encoding="utf-8"
    )
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "summary"])

    envelope = _machine(result)
    assert envelope["exit_code"] == 2
    error = envelope["payload"]["error"]
    assert document in error and "an array" in error

    # Without machine mode it is the same refusal the other unanswerable
    # reads make: a `FatalRtlBuddyError` carrying the path, not a traceback
    # from somewhere inside the reader.
    runner, rb = _runner()
    rendered = runner.invoke(rb.app, ["phys", "summary"])
    assert rendered.exit_code != 0
    assert isinstance(rendered.exception, PhysQueryError)
    assert document in str(rendered.exception)


@pytest.mark.parametrize(
    "document, field, malformed, described",
    [
        (MANIFEST_FILENAME, "synth", [], "an array"),
        # A list-valued `phys_dir` passed the block check until #563
        # round 7 and then raised `TypeError` out of `project_root_for`,
        # which is a traceback and no envelope.
        (MANIFEST_FILENAME, "phys_dir", ["artefacts"], "an array"),
        ("phys-model.json", "modules", 7, "a number"),
        ("phys-model.json", "totals", "x", "a string"),
        (
            "phys-model.json",
            "instances",
            ["u_sub/_64_"],
            "an array whose rows are not all objects",
        ),
    ],
)
def test_a_document_whose_blocks_are_the_wrong_shape_yields_an_error_envelope(
    phys_project, document, field, malformed, described
):
    """The finding (#561 review, Codex P2). The version check passes a
    document whose blocks are the wrong shape straight into the builders,
    where the failure is a `TypeError` or an `AttributeError` — a traceback
    and no envelope, which is the one thing an agent surface cannot read."""
    path = phys_project / "verif" / "blk" / "artefacts" / "both" / document
    body = json.loads(path.read_text(encoding="utf-8"))
    body[field] = malformed
    path.write_text(json.dumps(body), encoding="utf-8")
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "summary"])

    envelope = _machine(result)
    assert envelope["exit_code"] == 2
    error = envelope["payload"]["error"]
    assert document in error
    assert f"`{field}`" in error
    assert described in error


# --- module -----------------------------------------------------------------


def test_phys_module_reports_a_liberty_cells_instance_power(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "module", "DFF_X1"])

    payload = _machine(result)["payload"]
    assert payload["namespaces"] == ["liberty"]
    assert payload["row"] is None
    assert payload["instance_count"] == 2
    assert payload["power"]["total_uw"] == pytest.approx(3.171)


def test_phys_module_reports_an_rtl_modules_own_row(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "module", "sub"])

    payload = _machine(result)["payload"]
    assert payload["namespaces"] == ["rtl"]
    assert payload["row"] == {"module": "sub", "cell_count": 40, "area_um2": 96.0}


def test_phys_module_names_both_namespaces_on_a_collision(phys_project):
    """The finding (#561 review, Codex P2): one name, two measurements of two
    different things. The payload says so and the CLI prints it, rather than
    showing a module's cells and area beside a cell type's power as though
    they were one block's numbers."""
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        [
            "--machine",
            "phys",
            "module",
            "sub",
            "--phys-dir",
            "verif/blk/artefacts/collision",
        ],
    )

    payload = _machine(result)["payload"]
    assert payload["namespaces"] == ["rtl", "liberty"]
    assert payload["instance_join"] == INSTANCE_JOIN_NAME_COLLISION

    runner, rb = _runner()
    rendered = runner.invoke(
        rb.app,
        ["phys", "module", "sub", "--phys-dir", "verif/blk/artefacts/collision"],
    )
    assert rendered.exit_code == 0, rendered.output
    assert "name collision" in _flat(rendered.output)


def test_phys_module_renders_its_instances(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "module", "DFF_X1"])

    assert result.exit_code == 0, result.output
    assert "instances of DFF_X1: 2/2" in _flat(result.output)
    # The table ellipsizes a path too long for the column, as the coverage
    # tables do; the prefix is what a reader matches on.
    assert "u_sub/u_leaf" in result.output


def test_phys_module_prints_the_join_note_instead_of_a_bare_empty_table(phys_project):
    """`blk` is an RTL module; every instance row names a Liberty cell, so
    the join misses and the user must be told that rather than shown an
    empty instances section."""
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "module", "blk"])

    assert result.exit_code == 0, result.output
    assert "liberty-cell names only" in _flat(result.output)
    assert "hierarchy join" in _flat(result.output)


def test_phys_module_carries_the_join_note_in_its_machine_payload(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "module", "blk"])

    payload = _machine(result)["payload"]
    assert payload["instance_count"] == 0
    assert payload["instance_join"] == INSTANCE_JOIN_LIBERTY_ONLY
    # A cell name the join does reach carries no note at all.
    runner, rb = _runner()
    reached = _machine(runner.invoke(rb.app, ["--machine", "phys", "module", "DFF_X1"]))
    assert reached["payload"]["instance_join"] is None


def test_phys_instance_accepts_the_dotted_spelling_of_a_stored_path(phys_project):
    """OpenSTA stores `/`; the hub pane and the RTL side spell the same
    path with `.`, and pasting one into the other must still resolve."""
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "instance", "u_sub._64_"])

    payload = _machine(result)["payload"]
    assert payload["match"] == "exact"
    assert payload["instance"]["instance_path"] == "u_sub/_64_"


def test_a_negative_limit_is_rejected_rather_than_silently_meaning_all(phys_project):
    """`0` means every row, so a negative value is a typo, not a request —
    and the help says `min=0`, which the parser now agrees with."""
    for verb, extra in (
        ("summary", []),
        ("module", ["sub"]),
        ("instance", ["u_sub"]),
    ):
        runner, rb = _runner()
        result = runner.invoke(rb.app, ["phys", verb, *extra, "--limit", "-1"])
        assert result.exit_code == 2, result.output


def test_phys_module_machine_payload_honours_the_limit(phys_project):
    """The finding (#561 review, Codex P2). Under `--machine` the payload
    *is* the output, so a `--limit` the console honoured and the payload
    ignored was the flag lying to the one consumer that cannot re-count."""
    runner, rb = _runner()
    complete = _machine(
        runner.invoke(rb.app, ["--machine", "phys", "module", "DFF_X1"])
    )["payload"]
    assert len(complete["instances"]) == 2
    assert complete["limit"] == phys_query_mod.DEFAULT_RANK_LIMIT

    runner, rb = _runner()
    headed = _machine(
        runner.invoke(rb.app, ["--machine", "phys", "module", "DFF_X1", "--limit", "1"])
    )["payload"]
    assert [row["instance_path"] for row in headed["instances"]] == ["u_sub/_64_"]
    assert headed["limit"] == 1
    # A headed list is self-describing, and the total is the whole cell
    # type's rather than the listed rows'.
    assert headed["instance_count"] == 2
    assert headed["power"] == complete["power"]

    runner, rb = _runner()
    everything = _machine(
        runner.invoke(rb.app, ["--machine", "phys", "module", "DFF_X1", "--limit", "0"])
    )["payload"]
    assert len(everything["instances"]) == 2


def test_phys_module_console_counts_the_instances_it_did_not_list(phys_project):
    """The table is now the payload's own list, so the `n/total` line has
    to come from `instance_count` rather than from what it was handed."""
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "module", "DFF_X1", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "instances of DFF_X1: 1/2" in _flat(result.output)


def test_phys_instance_machine_payload_honours_the_limit(phys_project):
    """The finding (#561 review, Codex P2), the subtree half of it."""
    runner, rb = _runner()
    complete = _machine(
        runner.invoke(rb.app, ["--machine", "phys", "instance", "u_sub"])
    )["payload"]
    assert len(complete["children"]) == complete["child_count"] == 2
    assert complete["limit"] == phys_query_mod.DEFAULT_RANK_LIMIT

    runner, rb = _runner()
    headed = _machine(
        runner.invoke(
            rb.app, ["--machine", "phys", "instance", "u_sub", "--limit", "1"]
        )
    )["payload"]
    assert [row["instance_path"] for row in headed["children"]] == ["u_sub/_64_"]
    assert headed["limit"] == 1
    assert headed["child_count"] == 2
    # The rollup is the subtree's, listed or not.
    assert headed["rollup"] == complete["rollup"]

    runner, rb = _runner()
    everything = _machine(
        runner.invoke(
            rb.app, ["--machine", "phys", "instance", "u_sub", "--limit", "0"]
        )
    )["payload"]
    assert len(everything["children"]) == 2


def test_phys_instance_console_says_how_many_children_it_left_out(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "instance", "u_sub", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "1/2 children shown; --limit 0 for all" in _flat(result.output)


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
    assert "did you mean" in _flat(result.output)


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
    # No area: the model has no per-cell area to sum, and joining the
    # module's total in once per leaf would multiply it (Phase 5 owns the
    # hierarchy join that can answer this).
    assert "area_um2" not in payload["rollup"]
    assert "modules_matched" not in payload["rollup"]


def test_phys_instance_renders_the_rollup(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "instance", "u_sub"])

    assert result.exit_code == 0, result.output
    assert "prefix match, 2 leaf instance(s)" in _flat(result.output)
    assert "rollup (2)" in _flat(result.output)
    assert "subtree area" not in _flat(result.output)


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


def test_the_missing_half_note_offers_the_merge_only_when_it_can_happen(
    phys_project,
):
    """A power half that recorded a netlist hash is one a later `rb synth`
    can merge onto, so the plain note is the true one."""
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        ["phys", "instance", "u_sub", "--phys-dir", "verif/blk/artefacts/power_only"],
    )

    assert result.exit_code == 0, result.output
    assert (
        "no per-module rows in this model - run `rb synth` into the same "
        "artefact directory to add them" in _flat(result.output)
    )


def test_the_missing_half_note_never_promises_an_impossible_merge(phys_project):
    """The finding (#561 round-10 review, Codex P2). A `netlist-source: pnr`
    power half records no netlist hash, so the symmetric provenance gate
    makes a later `rb synth` *replace* the model rather than complete it —
    and the note was sending the reader to destroy the rows they have."""
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        ["phys", "instance", "u_sub", "--phys-dir", "verif/blk/artefacts/pnr_power"],
    )

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "run `rb synth` into the same artefact directory" not in flat
    assert "would replace it rather than complete it" in flat
    assert "the power half records no netlist hash to pair on" in flat
    assert "re-run `rb power` on the netlist it writes" in flat


def test_phys_instance_says_which_half_is_missing(phys_project):
    """The verb reads a power-only model perfectly well, and the reader still
    needs telling that `area` and the module rows are one command away — the
    same note `summary` and `module` print."""
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        ["phys", "instance", "u_sub", "--phys-dir", "verif/blk/artefacts/power_only"],
    )

    assert result.exit_code == 0, result.output
    assert "no per-module rows in this model" in _flat(result.output)
    assert "rb synth" in _flat(result.output)


# --- rb phys runs (#568) ----------------------------------------------------


def test_phys_runs_lists_every_run_newest_first(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "runs"])

    envelope = _machine(result)
    assert envelope["command"] == "phys runs"
    assert envelope["exit_code"] == 0
    payload = envelope["payload"]
    assert payload["schema_version"] == phys_query_mod.PHYS_QUERY_SCHEMA_VERSION
    assert [entry["run"] for entry in payload["runs"]] == [
        "both",
        "collision",
        "pnr_power",
        "power_only",
        "old_synth",
    ]
    assert payload["count"] == 5
    newest = payload["runs"][0]
    assert newest["newest"] is True
    assert newest["phys_dir"] == "verif/blk/artefacts/both"
    assert newest["backends"] == {"synth": "yosys", "power": "openroad"}


def test_phys_runs_reports_the_power_mode_and_the_experiment_identity(phys_project):
    """The reason the verb exists: two runs of one design differ by their
    configuration and their stimulus, not by their top."""
    runner, rb = _runner()

    payload = _machine(runner.invoke(rb.app, ["--machine", "phys", "runs"]))["payload"]

    newest = payload["runs"][0]
    assert newest["mode"] == "dynamic"
    assert newest["activity"]["label"] == "saif csr_smoke"
    assert newest["fingerprint"].startswith("nangate45 · timing-opt · opts ")
    # A run that recorded none of it says so with nulls, not with zeros
    # or with an invented default.
    older = next(entry for entry in payload["runs"] if entry["run"] == "power_only")
    assert older["mode"] is None and older["activity"] is None
    assert older["fingerprint"] is None


def test_phys_runs_renders_a_table_naming_the_default_run(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "runs"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    # The directories are printed under the table, not in it: a table
    # cell wraps or ellipsises a long path, and this is the one value a
    # reader copies into the next command.
    assert "verif/blk/artefacts/both" in flat
    assert "verif/blk/artefacts/old_synth" in flat
    assert "* the newest run" in flat
    assert "rb phys summary --phys-dir" in flat


def test_the_backends_cell_names_only_the_halves_that_ran():
    """`none` in a column about what produced this run's numbers is two
    words of padding for something the absence already says."""
    assert RtlBuddy._phys_backends({"synth": "yosys", "power": "openroad"}) == (
        "yosys+openroad"
    )
    assert RtlBuddy._phys_backends({"synth": None, "power": "openroad"}) == "openroad"
    assert RtlBuddy._phys_backends({"synth": None, "power": None}) == "-"


def test_the_power_cell_pairs_the_mode_with_what_drove_it():
    """The label is the payload's, so the table, the MCP answer and the
    pane's dropdown say the same words about the same run."""
    assert (
        RtlBuddy._phys_power_cell(
            {"mode": "dynamic", "activity": {"label": "saif csr_smoke"}}
        )
        == "dynamic (saif csr_smoke)"
    )
    # A document from before the mode was recorded says what it knows
    # rather than inventing the half it does not.
    assert RtlBuddy._phys_power_cell({"mode": "static", "activity": None}) == "static"
    assert RtlBuddy._phys_power_cell({"mode": None, "activity": None}) == "-"


def test_phys_runs_heads_the_list_and_says_it_did(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "runs", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert "2/5 runs shown; --limit 0 for all" in _flat(result.output)


def test_phys_runs_on_a_project_with_no_artefacts_is_not_an_error(
    tmp_path, monkeypatch
):
    """The other three verbs exit 2 — they were asked about a run. This one
    is asking what runs there are, and "none" is an answer."""
    root = tmp_path / "empty"
    root.mkdir()
    shutil.copy(_FIXTURES / "minimal_project" / "root_config.yaml", root)
    monkeypatch.chdir(root)
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "runs"])

    assert result.exit_code == 0, result.output
    assert "rb synth" in result.output and "rb power" in result.output


def test_phys_runs_machine_payload_is_the_builders_verbatim(phys_project):
    runner, rb = _runner()

    payload = _machine(runner.invoke(rb.app, ["--machine", "phys", "runs"]))["payload"]

    assert payload == phys_query_mod.runs_payload(
        str(phys_project), limit=phys_query_mod.DEFAULT_RUNS_LIMIT
    )


# --- the read verbs write nothing, including the log -------------------------


def _log_is_writable_only_by_owner(path: Path) -> bool:
    """Whether ``chmod`` actually denies this process a write.

    Root ignores the permission bits, and some CI images run as root, so
    the test below would pass there for the wrong reason (nothing was
    refused because nothing could be). Checked rather than assumed.
    """
    try:
        with path.open("a"):
            return False
    except PermissionError:
        return True


def test_a_read_verb_does_not_open_the_project_log_for_writing(phys_project):
    """A read verb answers from artefacts on disk and writes nothing —
    the log included.

    The file handler is opened for writing and a process's first open of
    a path truncates it, so attaching one here failed a read in a
    read-only checkout and, worse, silently emptied the log of the run
    the reader was asking about. `list_only=True` now skips it (#561).
    """
    log = phys_project / "rtl_buddy.log"
    log.write_text("the flow that produced these artefacts said this\n")
    log.chmod(0o444)
    if not _log_is_writable_only_by_owner(log):  # pragma: no cover - root CI
        pytest.skip("this process can write a read-only file (running as root?)")
    runner, rb = _runner()

    try:
        result = runner.invoke(rb.app, ["phys", "summary"])
    finally:
        log.chmod(0o644)

    assert result.exit_code == 0, result.output
    assert "verif/blk/artefacts/both/phys-manifest.json" in _flat(result.output)
    assert log.read_text() == "the flow that produced these artefacts said this\n"


def test_a_read_verb_leaves_the_previous_run_s_log_intact(phys_project):
    """The truncating half of the same bug, on a writable log.

    Asking what the last run measured is the moment its log matters
    most; a read that empties it takes away the evidence it was called
    to explain.
    """
    log = phys_project / "rtl_buddy.log"
    log.write_text("power: OpenROAD reported 3.171 uW\n")
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "module", "blk"])

    assert result.exit_code == 0, result.output
    assert log.read_text() == "power: OpenROAD reported 3.171 uW\n"
