"""Tests for the `rb phys` verbs and the artefacts they read.

Pinned behaviour:

- `rb phys summary` / `module` / `instance` answer from a run's `phys-manifest.json` and
  the model it names, and run nothing;
- their `--machine` payloads are exactly the dicts the payload builders return;
- discovery precedence is an explicit `--manifest`, then `--phys-dir`, then the newest
  manifest under the project root;
- a model with only one half names the missing half and the command that produces it,
  rather than reporting zeros;
- an unknown module or instance exits 2 with near misses, and a project with no
  artefacts exits 2 naming the commands that write them.

Console assertions read `result.output`, not caplog. Any assertion on more than one word
goes through `_flat()`, because Rich wraps at the terminal width.
"""

from __future__ import annotations

import json
import re
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

# Leaves name Liberty cells and synthesis rows name RTL modules; the namespaces are kept
# apart so `_COLLIDING_INSTANCES` is a real collision.
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


# A design whose Liberty cell is named after one of its RTL modules.
_COLLIDING_INSTANCES = [{**row, "module": "sub"} for row in _INSTANCES]


# Both halves of a fixture run record the same netlist hash, as an `rb synth` then `rb
# power` pair does; the merge requires it before either half inherits the other (see
# :func:`rtl_buddy.phys.model.may_inherit_other_half`).
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
    # A power run off a routed database has no netlist to hash, so no later synthesis
    # can pair with it.
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
    """The console output with Rich's wrapping folded away.

    Rich wraps to the terminal's width, which differs between CI and a developer
    machine, so multi-word console assertions belong here.
    """
    return " ".join(output.split())


def _machine(result) -> dict:
    assert result.exit_code in (0, 1, 2), result.output
    return json.loads(result.output.strip().splitlines()[-1])


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


def test_phys_summary_limit_heads_both_rankings_and_says_so(phys_project):
    """`--limit` still heads both rankings, and the payload reports each ranking's own
    limit.
    """
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "summary", "--limit", "1"])

    payload = _machine(result)["payload"]
    assert payload["limit"] == 1
    assert payload["limits"] == {"modules": 1, "instances": 1}


def test_phys_summary_instances_limit_none_drops_the_instance_rows(phys_project):
    """`--instances-limit none` drops the instance rows while `counts` still reports how
    many rows the model holds.
    """
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        [
            "--machine",
            "phys",
            "summary",
            "--limit",
            "0",
            "--instances-limit",
            "none",
        ],
    )

    payload = _machine(result)["payload"]
    assert [row["module"] for row in payload["modules"]] == ["blk", "sub"]
    assert payload["instances"] == []
    assert payload["limits"] == {"modules": 0, "instances": "none"}
    # The model's row counts and the halves block are unchanged by suppression.
    assert payload["counts"] == {"modules": 2, "instances": 2}
    assert payload["missing_halves"] == []


def test_phys_summary_modules_limit_overrides_the_shared_limit(phys_project):
    """Each override is per ranking: heading one leaves the other alone."""
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        ["--machine", "phys", "summary", "--limit", "0", "--modules-limit", "1"],
    )

    payload = _machine(result)["payload"]
    assert len(payload["modules"]) == 1
    assert len(payload["instances"]) == 2
    assert payload["limits"] == {"modules": 1, "instances": 0}


def test_phys_summary_modules_limit_none_renders_no_module_table(phys_project):
    """The console suppresses the same list the payload does."""
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "summary", "--modules-limit", "none"])

    assert result.exit_code == 0, result.output
    assert "Heaviest Modules" not in _flat(result.output)
    assert "Hottest Instances" in _flat(result.output)


@pytest.mark.parametrize("bad", ["all", "-1", ""])
def test_phys_summary_rejects_a_per_ranking_limit_it_cannot_read(phys_project, bad):
    """An unreadable per-ranking limit is a usage error, not a traceback or a silent
    fallback.
    """
    runner, rb = _runner()

    result = runner.invoke(
        rb.app, ["--machine", "phys", "summary", "--instances-limit", bad]
    )

    assert result.exit_code == 2, result.output
    # The usage error is click's own rendering, which colours the flag name piecewise
    # when the console is forced to colour.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "instances-limit" in _flat(plain)


def test_phys_summary_without_the_overrides_is_unchanged(phys_project):
    """Without the overrides the ranking pair and default limit are unchanged."""
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "summary"])

    payload = _machine(result)["payload"]
    assert payload["limit"] == phys_query_mod.DEFAULT_RANK_LIMIT
    assert payload["limits"] == {
        "modules": phys_query_mod.DEFAULT_RANK_LIMIT,
        "instances": phys_query_mod.DEFAULT_RANK_LIMIT,
    }
    assert len(payload["modules"]) == 2
    assert len(payload["instances"]) == 2


def test_summary_payload_does_not_rank_a_suppressed_half(phys_project):
    """A suppressed ranking is not sorted: the builder is given rows that raise if
    sorted.
    """
    ctx = phys_query_mod.load_context(str(phys_project))

    class _Explodes(dict):
        def get(self, key, default=None):
            raise AssertionError(f"ranked a suppressed half: {key}")

    ctx.model["instances"] = [_Explodes(), _Explodes()]

    payload = phys_query_mod.summary_payload(
        ctx, limit=0, instances_limit=phys_query_mod.RANK_NONE
    )

    assert payload["instances"] == []
    # The count is a len(), not a read of the rows.
    assert payload["counts"]["instances"] == 2


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
    """A JSON root that is not an object yields an error envelope under `--machine`."""
    (phys_project / "verif" / "blk" / "artefacts" / "both" / document).write_text(
        "[]", encoding="utf-8"
    )
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "summary"])

    envelope = _machine(result)
    assert envelope["exit_code"] == 2
    error = envelope["payload"]["error"]
    assert document in error and "an array" in error

    # Without machine mode it raises a `FatalRtlBuddyError` carrying the path.
    runner, rb = _runner()
    rendered = runner.invoke(rb.app, ["phys", "summary"])
    assert rendered.exit_code != 0
    assert isinstance(rendered.exception, PhysQueryError)
    assert document in str(rendered.exception)


@pytest.mark.parametrize(
    "document, field, malformed, described",
    [
        (MANIFEST_FILENAME, "synth", [], "an array"),
        # A list-valued `phys_dir` yields an error envelope, not a `TypeError`.
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
    """A document whose blocks have the wrong shape yields an error envelope, not a
    traceback.
    """
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
    """One name that is both a module and a cell type: the payload and CLI name both
    namespaces instead of mixing their numbers.
    """
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
    # The table ellipsizes a path too long for the column; the prefix is what a reader
    # matches on.
    assert "u_sub/u_leaf" in result.output


def test_phys_module_prints_the_join_note_instead_of_a_bare_empty_table(phys_project):
    """When the join misses because every instance row names a Liberty cell, the CLI
    prints a join note instead of an empty instances section.
    """
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
    # A cell name the join reaches carries no note.
    runner, rb = _runner()
    reached = _machine(runner.invoke(rb.app, ["--machine", "phys", "module", "DFF_X1"]))
    assert reached["payload"]["instance_join"] is None


def test_phys_instance_accepts_the_dotted_spelling_of_a_stored_path(phys_project):
    """OpenSTA stores `/`; the dotted spelling of the same path also resolves."""
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["--machine", "phys", "instance", "u_sub._64_"])

    payload = _machine(result)["payload"]
    assert payload["match"] == "exact"
    assert payload["instance"]["instance_path"] == "u_sub/_64_"


def test_a_negative_limit_is_rejected_rather_than_silently_meaning_all(phys_project):
    """A negative limit is rejected: `0` means every row and the help says `min=0`."""
    for verb, extra in (
        ("summary", []),
        ("module", ["sub"]),
        ("instance", ["u_sub"]),
    ):
        runner, rb = _runner()
        result = runner.invoke(rb.app, ["phys", verb, *extra, "--limit", "-1"])
        assert result.exit_code == 2, result.output


def test_phys_module_machine_payload_honours_the_limit(phys_project):
    """Under `--machine` the payload honours `--limit`."""
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
    # A headed list is self-describing, and the total is the whole cell type's rather
    # than the listed rows'.
    assert headed["instance_count"] == 2
    assert headed["power"] == complete["power"]

    runner, rb = _runner()
    everything = _machine(
        runner.invoke(rb.app, ["--machine", "phys", "module", "DFF_X1", "--limit", "0"])
    )["payload"]
    assert len(everything["instances"]) == 2


def test_phys_module_console_counts_the_instances_it_did_not_list(phys_project):
    """The `n/total` line comes from `instance_count`, not from the listed rows."""
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "module", "DFF_X1", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "instances of DFF_X1: 1/2" in _flat(result.output)


def test_phys_instance_machine_payload_honours_the_limit(phys_project):
    """Under `--machine` the instance payload's subtree rows honour the limit."""
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
    # No area: the model has no per-cell area to sum, and joining the module's total
    # once per leaf would multiply it.
    assert "area_um2" not in payload["rollup"]
    assert "modules_matched" not in payload["rollup"]


def test_phys_instance_renders_the_rollup(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "instance", "u_sub"])

    assert result.exit_code == 0, result.output
    assert "prefix match, 2 leaf instance(s)" in _flat(result.output)
    assert "rollup (2)" in _flat(result.output)
    assert "subtree area" not in _flat(result.output)


# `u_blk` is a measured leaf and a prefix of two more rows, the shape
# `instance_payload`'s docstring allows.
_LEAF_WITH_DESCENDANTS = [
    {"instance_path": "u_blk", "module": "DFF_X1", "total_uw": 4.0},
    {"instance_path": "u_blk/_1_", "module": "INV_X1", "total_uw": 1.0},
    {"instance_path": "u_blk/u_deep/_2_", "module": "INV_X1", "total_uw": 2.0},
]


def test_phys_instance_console_does_not_describe_a_subtree_it_did_not_sum(
    phys_project,
):
    """An exact match sums its own row, and the header says why the rows below it are
    listed instead of describing an unsummed subtree.
    """
    phys_dir = _write_run(
        phys_project, "both_shapes", instances=_LEAF_WITH_DESCENDANTS, mtime=3_200_000
    )
    runner, rb = _runner()

    result = runner.invoke(
        rb.app, ["phys", "instance", "u_blk", "--phys-dir", str(phys_dir)]
    )

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "exact match, 1 leaf instance(s)" in flat
    assert "rollup (1)" in flat
    assert "2 row(s) below this path are listed for navigation" in flat


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
    """A power half that recorded a netlist hash can be merged onto by a later `rb
    synth`, so the plain note is the true one.
    """
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
    """A `netlist-source: pnr` power half records no netlist hash, so a later `rb synth`
    would replace the model; the note must not offer that merge.
    """
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
    """`rb phys instance` on a power-only model says that `area` and the module rows are
    one command away, like `summary` and `module`.
    """
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        ["phys", "instance", "u_sub", "--phys-dir", "verif/blk/artefacts/power_only"],
    )

    assert result.exit_code == 0, result.output
    assert "no per-module rows in this model" in _flat(result.output)
    assert "rb synth" in _flat(result.output)


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
    """Two runs of one design differ by configuration and stimulus, so `runs` reports
    the power mode and experiment identity.
    """
    runner, rb = _runner()

    payload = _machine(runner.invoke(rb.app, ["--machine", "phys", "runs"]))["payload"]

    newest = payload["runs"][0]
    assert newest["mode"] == "dynamic"
    assert newest["activity"]["label"] == "saif csr_smoke"
    assert newest["fingerprint"].startswith("nangate45 · timing-opt · opts ")
    # A run that recorded none of it reports nulls, not zeros or an invented default.
    older = next(entry for entry in payload["runs"] if entry["run"] == "power_only")
    assert older["mode"] is None and older["activity"] is None
    assert older["fingerprint"] is None


def test_phys_runs_renders_a_table_naming_the_default_run(phys_project):
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "runs"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    # Directories are printed under the table because a table cell wraps or ellipsizes a
    # long path.
    assert "verif/blk/artefacts/both" in flat
    assert "verif/blk/artefacts/old_synth" in flat
    assert "* the newest run" in flat
    assert "rb phys summary --phys-dir" in flat


def test_the_backends_cell_names_only_the_halves_that_ran():
    """The backends cell names only the halves that ran."""
    assert RtlBuddy._phys_backends({"synth": "yosys", "power": "openroad"}) == (
        "yosys+openroad"
    )
    assert RtlBuddy._phys_backends({"synth": None, "power": "openroad"}) == "openroad"
    assert RtlBuddy._phys_backends({"synth": None, "power": None}) == "-"


def test_the_power_cell_pairs_the_mode_with_what_drove_it():
    """The power cell pairs the mode with what drove it, using the payload's label."""
    assert (
        RtlBuddy._phys_power_cell(
            {"mode": "dynamic", "activity": {"label": "saif csr_smoke"}}
        )
        == "dynamic (saif csr_smoke)"
    )
    # A document that predates the recorded mode reports only what it knows.
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
    """`rb phys runs` on a project with no artefacts lists none and exits 0; the other
    verbs exit 2.
    """
    root = tmp_path / "empty"
    root.mkdir()
    shutil.copy(_FIXTURES / "minimal_project" / "root_config.yaml", root)
    monkeypatch.chdir(root)
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "runs"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "rb synth" in flat and "rb power" in flat


def test_phys_runs_machine_payload_is_the_builders_verbatim(phys_project):
    runner, rb = _runner()

    payload = _machine(runner.invoke(rb.app, ["--machine", "phys", "runs"]))["payload"]

    assert payload == phys_query_mod.runs_payload(
        str(phys_project), limit=phys_query_mod.DEFAULT_RUNS_LIMIT
    )


def _log_is_writable_only_by_owner(path: Path) -> bool:
    """Whether ``chmod`` actually denies this process a write.

    Root ignores permission bits, so the test below would pass as root for the wrong
    reason.
    """
    try:
        with path.open("a"):
            return False
    except PermissionError:
        return True


def test_a_read_verb_does_not_open_the_project_log_for_writing(phys_project):
    """A read verb writes nothing, the log included.

    Opening the log file handler for writing truncates it, which fails in a read-only
    checkout and empties the log of the run being asked about. Read verbs skip it via
    `list_only=True`.
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
    """A read verb leaves the previous run's log intact on a writable log."""
    log = phys_project / "rtl_buddy.log"
    log.write_text("power: OpenROAD reported 3.171 uW\n")
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["phys", "module", "blk"])

    assert result.exit_code == 0, result.output
    assert log.read_text() == "power: OpenROAD reported 3.171 uW\n"
