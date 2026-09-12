"""
Unit tests for the `rb phys` payload builders (#558).

These are the dicts the CLI prints under `--machine` and a later MCP tool
wraps verbatim, so they are asserted on directly rather than through the
CLI. The artefacts are written with the phase-1 producers rather than by
hand: a payload test that invented its own document shape would keep
passing after the producers stopped writing that shape.
"""

import os

import pytest

from rtl_buddy.phys.manifest import (
    MANIFEST_FILENAME,
    build_manifest,
    write_manifest,
)
from rtl_buddy.phys.model import (
    build_power_model,
    build_synth_model,
    merge_model,
    write_model,
)
from rtl_buddy.phys.query import (
    INSTANCE_JOIN_LIBERTY_ONLY,
    PHYS_QUERY_SCHEMA_VERSION,
    PhysQueryError,
    heaviest_modules,
    hottest_instances,
    instance_payload,
    is_descendant,
    level_path,
    load_context,
    module_names,
    module_payload,
    resolve_manifest_path,
    summary_payload,
)

MODULE_ROWS = [
    {"module": "blk", "cell_count": 120, "area_um2": 480.5},
    {"module": "sub", "cell_count": 40, "area_um2": 96.0},
    {"module": "tiny", "cell_count": 2, "area_um2": None},
]

INSTANCE_ROWS = [
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
        "module": "tiny",
        "leakage_uw": 0.001,
        "internal_uw": 0.5,
        "switching_uw": 0.25,
        "total_uw": 0.751,
    },
    {
        "instance_path": "u_other/_9_",
        "module": "sub",
        "leakage_uw": 0.5,
        "internal_uw": 8.0,
        "switching_uw": 1.5,
        "total_uw": 10.0,
    },
]


def _write_run(root, run, *, top="blk", modules=None, instances=None, mtime=None):
    """One run's artefact directory, written the way the producers do."""
    phys_dir = root / "verif" / "blk" / "artefacts" / run
    phys_dir.mkdir(parents=True, exist_ok=True)

    model = None
    if modules is not None:
        model = build_synth_model(
            top=top, modules=modules, area_um2=576.5, gate_count=162
        )
    if instances is not None:
        power = build_power_model(
            top=top,
            instances=instances,
            internal_w=10.78e-6,
            switching_w=1.8175e-6,
            leakage_w=0.58e-6,
            total_w=13.171e-6,
        )
        model = (
            merge_model(model, power, own_half="instances")
            if model is not None
            else power
        )
    model_path = write_model(model, phys_dir)

    command = "power" if instances is not None else "synth"
    manifest = build_manifest(
        project_root=root,
        phys_dir=phys_dir,
        command=command,
        run=run,
        top=top,
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
    )
    manifest_path = write_manifest(manifest, phys_dir)
    if mtime is not None:
        os.utime(manifest_path, (mtime, mtime))
    return phys_dir


@pytest.fixture
def project(tmp_path):
    """A project with two runs: a complete model and an older synth-only one."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    _write_run(root, "old_synth", modules=MODULE_ROWS, mtime=1_000_000)
    _write_run(
        root, "both", modules=MODULE_ROWS, instances=INSTANCE_ROWS, mtime=2_000_000
    )
    return root


# --- discovery --------------------------------------------------------------


def test_discovery_picks_the_newest_manifest(project):
    """No override means the last run that measured anything."""
    ctx = load_context(project)

    assert ctx.manifest["run"] == "both"


def test_phys_dir_overrides_discovery(project):
    ctx = load_context(
        project, phys_dir=project / "verif" / "blk" / "artefacts" / "old_synth"
    )

    assert ctx.manifest["run"] == "old_synth"


def test_explicit_manifest_beats_phys_dir(project):
    """The most specific request wins, even when both are given."""
    newest = project / "verif" / "blk" / "artefacts" / "both" / MANIFEST_FILENAME

    ctx = load_context(
        project,
        phys_dir=project / "verif" / "blk" / "artefacts" / "old_synth",
        manifest=newest,
    )

    assert ctx.manifest["run"] == "both"


def test_a_manifest_option_naming_a_directory_is_accepted(project):
    ctx = load_context(
        project, manifest=project / "verif" / "blk" / "artefacts" / "old_synth"
    )

    assert ctx.manifest["run"] == "old_synth"


def test_missing_manifest_names_the_commands_that_write_one(tmp_path):
    with pytest.raises(PhysQueryError) as excinfo:
        resolve_manifest_path(tmp_path)

    message = str(excinfo.value)
    assert MANIFEST_FILENAME in message
    assert "rb synth" in message and "rb power" in message


def test_a_named_directory_without_a_manifest_says_so(project):
    empty = project / "verif" / "blk" / "artefacts" / "nothing"
    empty.mkdir()

    with pytest.raises(PhysQueryError) as excinfo:
        resolve_manifest_path(project, phys_dir=empty)

    assert "run `rb synth` or `rb power` there first" in str(excinfo.value)


def test_a_manifest_naming_a_vanished_model_is_an_error(project):
    phys_dir = project / "verif" / "blk" / "artefacts" / "both"
    (phys_dir / "phys-model.json").unlink()

    with pytest.raises(PhysQueryError) as excinfo:
        load_context(project)

    assert "names no physical model" in str(excinfo.value)


# --- summary ----------------------------------------------------------------


def test_summary_reports_the_header_totals_and_both_rankings(project):
    payload = summary_payload(load_context(project))

    assert payload["schema_version"] == PHYS_QUERY_SCHEMA_VERSION
    assert payload["run_command"] == "power"
    assert payload["run"] == "both"
    assert payload["top"] == "blk"
    assert payload["backends"] == {"synth": "yosys", "power": "openroad"}
    assert payload["manifest"] == "verif/blk/artefacts/both/phys-manifest.json"
    assert payload["model"] == "verif/blk/artefacts/both/phys-model.json"
    assert payload["units"] == {"area": "um2", "power": "uW"}
    # The synth totals are the log scrape, the power totals the watts the
    # flow reported, scaled. Neither is a sum of the rows.
    assert payload["totals"]["cell_count"] == 162
    assert payload["totals"]["total_uw"] == pytest.approx(13.171)
    assert payload["counts"] == {"modules": 3, "instances": 3}
    assert [row["module"] for row in payload["modules"]] == ["blk", "sub", "tiny"]
    assert [row["instance_path"] for row in payload["instances"]] == [
        "u_other/_9_",
        "u_sub/_64_",
        "u_sub/u_leaf/_12_",
    ]
    assert payload["artefacts"]["synth_netlist"] == (
        "verif/blk/artefacts/both/synth_netlist.v"
    )
    assert payload["artefacts"]["power_instances"] == (
        "verif/blk/artefacts/both/power_instances.rpt"
    )


def test_summary_limit_truncates_both_rankings(project):
    payload = summary_payload(load_context(project), limit=1)

    assert len(payload["modules"]) == 1
    assert len(payload["instances"]) == 1
    assert payload["limit"] == 1


def test_summary_of_a_synth_only_model_names_the_missing_half(project):
    ctx = load_context(
        project, phys_dir=project / "verif" / "blk" / "artefacts" / "old_synth"
    )

    payload = summary_payload(ctx)

    assert payload["missing_halves"] == ["instances"]
    assert payload["halves"]["instances"] == {
        "present": False,
        "rows": None,
        "produced_by": "rb power",
    }
    assert payload["halves"]["modules"]["rows"] == 3
    assert payload["counts"]["instances"] is None
    assert payload["instances"] == []
    assert payload["backends"]["power"] is None


def test_rankings_sink_the_rows_nobody_measured():
    """A `null` metric is unmeasured, not smallest — it goes last."""
    model = {
        "modules": [
            {"module": "unmapped", "cell_count": None, "area_um2": None},
            {"module": "small", "cell_count": 1, "area_um2": 1.0},
        ],
        "instances": [
            {"instance_path": "b", "total_uw": None},
            {"instance_path": "a", "total_uw": 0.0},
        ],
    }

    assert [row["module"] for row in heaviest_modules(model)] == ["small", "unmapped"]
    assert [row["instance_path"] for row in hottest_instances(model)] == ["a", "b"]


# --- module -----------------------------------------------------------------


def test_module_payload_joins_the_synth_row_to_its_instances(project):
    payload = module_payload(load_context(project), "sub")

    assert payload["module"] == "sub"
    assert payload["row"] == {"module": "sub", "cell_count": 40, "area_um2": 96.0}
    assert [row["instance_path"] for row in payload["instances"]] == [
        "u_other/_9_",
        "u_sub/_64_",
    ]
    assert payload["instance_count"] == 2
    assert payload["power"]["total_uw"] == pytest.approx(12.42)
    assert payload["power"]["leakage_uw"] == pytest.approx(0.579)


def test_module_names_span_both_halves(project):
    """A liberty cell only the power half knows is still askable."""
    ctx = load_context(project)
    ctx.model["modules"] = [{"module": "blk", "cell_count": 1, "area_um2": None}]

    assert module_names(ctx.model) == ["blk", "sub", "tiny"]


def test_module_name_matching_is_case_insensitive(project):
    assert module_payload(load_context(project), "SUB")["module"] == "sub"


def test_module_payload_over_a_synth_only_model_has_no_instances(project):
    ctx = load_context(
        project, phys_dir=project / "verif" / "blk" / "artefacts" / "old_synth"
    )

    payload = module_payload(ctx, "sub")

    assert payload["instances"] is None
    assert payload["power"] is None
    assert payload["missing_halves"] == ["instances"]


def test_a_module_only_the_synth_half_knows_says_the_join_cannot_see_it(project):
    """ "No instances" and "the join misses" are different answers.

    `blk` is the top: the synthesis half has a row for it, and the power
    half — which is populated — names Liberty cells on its leaves, so
    nothing can ever match. Without the note the empty table reads as
    "the top burns no power".
    """
    payload = module_payload(load_context(project), "blk")

    assert payload["instances"] == []
    assert payload["instance_count"] == 0
    assert payload["instance_join"] == INSTANCE_JOIN_LIBERTY_ONLY
    assert "liberty-cell names only" in payload["instance_join"]


def test_a_module_the_join_does_reach_carries_no_note(project):
    """A Liberty cell name matches, so there is nothing to qualify."""
    assert module_payload(load_context(project), "sub")["instance_join"] is None


def test_a_synth_only_model_carries_no_join_note(project):
    """With no power half at all, `missing_halves` is the honest signal
    and a join note would only compete with it."""
    ctx = load_context(
        project, phys_dir=project / "verif" / "blk" / "artefacts" / "old_synth"
    )

    assert module_payload(ctx, "blk")["instance_join"] is None


def test_unknown_module_reports_near_misses(project):
    with pytest.raises(PhysQueryError) as excinfo:
        module_payload(load_context(project), "subb")

    assert "sub" in excinfo.value.candidates


def test_an_unknown_module_on_a_half_model_names_the_missing_command(project):
    ctx = load_context(
        project, phys_dir=project / "verif" / "blk" / "artefacts" / "old_synth"
    )

    with pytest.raises(PhysQueryError) as excinfo:
        module_payload(ctx, "nowhere")

    assert "run `rb power`" in str(excinfo.value)


# --- instance ---------------------------------------------------------------


def test_instance_payload_answers_an_exact_leaf(project):
    payload = instance_payload(load_context(project), "u_sub/_64_")

    assert payload["match"] == "exact"
    assert payload["instance"]["module"] == "sub"
    assert payload["children"] == []
    assert payload["rollup"]["instances"] == 1
    assert payload["rollup"]["total_uw"] == pytest.approx(2.42)
    # Area is joined from the synth half through the row's module.
    assert payload["rollup"]["area_um2"] == pytest.approx(96.0)
    assert payload["rollup"]["modules_matched"] == 1


def test_instance_payload_rolls_up_a_subtree_prefix(project):
    payload = instance_payload(load_context(project), "u_sub")

    assert payload["match"] == "prefix"
    assert payload["instance"] is None
    assert [row["instance_path"] for row in payload["children"]] == [
        "u_sub/_64_",
        "u_sub/u_leaf/_12_",
    ]
    assert payload["rollup"]["instances"] == 2
    assert payload["rollup"]["total_uw"] == pytest.approx(3.171)
    # `tiny` has no area, so only `sub` joins — reported, not hidden.
    assert payload["rollup"]["area_um2"] == pytest.approx(96.0)
    assert payload["rollup"]["modules_matched"] == 1


def test_a_prefix_must_end_on_a_separator(project):
    """`u_sub` must not claim `u_subsystem` — a different block."""
    assert is_descendant("u_sub/_64_", "u_sub")
    assert is_descendant("u_sub.x", "u_sub")
    assert not is_descendant("u_subsystem/_1_", "u_sub")
    assert not is_descendant("u_sub", "u_sub")


def test_descendancy_is_decided_on_levelled_paths():
    """Which separator a path is spelled with is the tool's choice, so it
    cannot be allowed to decide whether two paths are the same instance."""
    assert level_path("u_top.u_sub/_64_") == "u_top/u_sub/_64_"
    assert is_descendant("u_top/u_sub/_64_", "u_top.u_sub")
    assert is_descendant("u_top.u_sub._64_", "u_top/u_sub")
    # The boundary rule survives levelling.
    assert not is_descendant("u_subsystem._1_", "u_sub")


def test_a_dotted_query_finds_slash_stored_rows(project):
    """The model stores OpenSTA's `/`; the pane and the RTL side spell the
    same path with `.`, and a user pasting one must still get the row."""
    ctx = load_context(project)

    exact = instance_payload(ctx, "u_sub._64_")
    assert exact["match"] == "exact"
    assert exact["instance"]["instance_path"] == "u_sub/_64_"
    # The payload echoes what the user asked, not the model's spelling.
    assert exact["instance_path"] == "u_sub._64_"

    subtree = instance_payload(ctx, "u_sub")
    assert [row["instance_path"] for row in subtree["children"]] == [
        "u_sub/_64_",
        "u_sub/u_leaf/_12_",
    ]


def test_a_slash_query_finds_dot_stored_rows(project):
    ctx = load_context(project)
    ctx.model["instances"] = [
        {"instance_path": "u_top.u_sub._64_", "module": "sub", "total_uw": 1.0}
    ]

    payload = instance_payload(ctx, "u_top/u_sub")

    assert payload["match"] == "prefix"
    assert [row["instance_path"] for row in payload["children"]] == ["u_top.u_sub._64_"]


def test_unknown_instance_reports_near_misses(project):
    with pytest.raises(PhysQueryError) as excinfo:
        instance_payload(load_context(project), "u_sub/_65_")

    assert "u_sub/_64_" in excinfo.value.candidates


def test_instance_over_a_synth_only_model_names_the_power_command(project):
    ctx = load_context(
        project, phys_dir=project / "verif" / "blk" / "artefacts" / "old_synth"
    )

    with pytest.raises(PhysQueryError) as excinfo:
        instance_payload(ctx, "u_sub")

    assert "run `rb power`" in str(excinfo.value)
