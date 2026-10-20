"""`global-route:` options on a P&R platform and run, and the database a failed global route leaves (rtl_buddy#852)."""

import os
import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_pnr_checkpoints import _backend, _events, _pdk, _pnr_cfg
from test_pnr_repair import _render
from test_pnr_run_overrides import _platform, _run

from rtl_buddy.config.pnr import CHECKPOINT_STAGES
from rtl_buddy.config.pnr_global_route import (
    GlobalRoute,
    PnrGlobalRouteFile,
    read_global_route,
)
from rtl_buddy.config.pnr_platform import PnrRunOverrides
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.tools import pnr_abstract, pnr_checkpoints
from rtl_buddy.tools.pnr_openroad import _GLOBAL_ROUTE_CMD, OpenRoadPnr

#: Every option set, as a platform or run writes it.
_ALL = {
    "congestion_iterations": 100,
    "congestion_report_iter_step": 25,
    "grid_origin": [3, 3],
    "layer_adjustment": {"metal2": 0.9, "metal3": 0.8},
    "allow_congestion": True,
    "critical_nets_percentage": 30,
    "resistance_aware": True,
}

_FULL = (
    "global_route -congestion_iterations 100 -congestion_report_iter_step 25 "
    "-critical_nets_percentage 30 -resistance_aware -grid_origin {3 3} "
    "-allow_congestion -congestion_report_file $CONGESTION_RPT -verbose\n"
)
_START = "global_route -start_incremental -grid_origin {3 3} -allow_congestion\n"
_END = (
    "global_route -end_incremental -grid_origin {3 3} -allow_congestion "
    "-congestion_report_file $CONGESTION_RPT -verbose\n"
)
_GUIDES = (
    "write_guides $OUT_DIR/grid_origin.guide\nread_guides $OUT_DIR/grid_origin.guide\n"
)


def _gr(**kw) -> PnrGlobalRouteFile:
    return PnrGlobalRouteFile(**kw)


def _gr_platform(tmp_path, gr: dict | None = None, **kw):
    return _platform(
        _pdk(tmp_path), global_route=_gr(**gr) if gr is not None else None, **kw
    )


# -- configuration ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("congestion_iterations", 0, "congestion-iterations must be a positive"),
        ("congestion_iterations", True, "congestion-iterations must be a positive"),
        (
            "congestion_report_iter_step",
            -1,
            "congestion-report-iter-step must be a non-negative integer",
        ),
        ("grid_origin", [3], "grid-origin must be [x, y]"),
        ("grid_origin", [3, -1], "grid-origin must be [x, y]"),
        ("grid_origin", [1.5, 3], "grid-origin must be [x, y]"),
        ("layer_adjustment", 0.5, "layer-adjustment must map"),
        ("layer_adjustment", {"metal2": 1.5}, "layer-adjustment must map"),
        ("layer_adjustment", {"metal2 metal3": 0.5}, "layer-adjustment must map"),
        ("layer_adjustment", {"m{2}": 0.5}, "layer-adjustment must map"),
        ("allow_congestion", "yes", "allow-congestion must be true or false"),
        (
            "critical_nets_percentage",
            120,
            "critical-nets-percentage must be a number from 0 to 100",
        ),
        ("resistance_aware", 1, "resistance-aware must be true or false"),
    ],
)
def test_a_bad_value_is_refused_naming_the_key(field, value, message):
    with pytest.raises(FatalRtlBuddyError, match=re.escape(f"global-route.{message}")):
        read_global_route("pnr platform 'pl'", _gr(**{field: value}))


def test_every_option_reads_from_a_platform(tmp_path):
    gr = _gr_platform(tmp_path, _ALL).get_global_route()
    assert gr == GlobalRoute(
        congestion_iterations=100,
        congestion_report_iter_step=25,
        grid_origin=(3, 3),
        layer_adjustment=(("metal2", 0.9), ("metal3", 0.8)),
        allow_congestion=True,
        critical_nets_percentage=30.0,
        resistance_aware=True,
    )


def test_no_block_leaves_every_option_unset(tmp_path):
    assert _gr_platform(tmp_path).get_global_route().is_empty()


def test_a_run_wins_over_the_platform_field_by_field(tmp_path):
    platform = _gr_platform(
        tmp_path,
        {
            "congestion_iterations": 50,
            "grid_origin": [3, 3],
            "layer_adjustment": {"metal2": 0.9, "metal3": 0.8},
            "resistance_aware": True,
        },
    )
    run = _run(
        tmp_path,
        **{
            "global-route": {
                "congestion-iterations": 100,
                "layer-adjustment": {"metal4": 0.5},
                "resistance-aware": False,
            }
        },
    )
    gr = platform.with_run_overrides(run.get_overrides()).get_global_route()
    assert gr == GlobalRoute(
        congestion_iterations=100,
        grid_origin=(3, 3),
        layer_adjustment=(("metal4", 0.5),),
        resistance_aware=False,
    )
    # The platform itself is unchanged.
    assert platform.get_global_route().congestion_iterations == 50


def test_a_run_block_alone_applies_over_an_empty_platform(tmp_path):
    run = _run(tmp_path, **{"global-route": {"allow-congestion": True}})
    assert run.get_overrides() == PnrRunOverrides(
        global_route=GlobalRoute(allow_congestion=True)
    )
    gr = (
        _gr_platform(tmp_path)
        .with_run_overrides(run.get_overrides())
        .get_global_route()
    )
    assert gr == GlobalRoute(allow_congestion=True)


def test_a_bad_run_value_names_the_run(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="pnr run 'r': global-route.grid"):
        _run(tmp_path, **{"global-route": {"grid-origin": "3 3"}})


# -- rendering --------------------------------------------------------------------


def _base_template(template: str) -> str:
    """The flow template before `global-route:`: the flow's global route written out."""
    assert template.count("{{ global_route_cmd }}") == 1
    return template.replace("{{ global_route_cmd }}", _GLOBAL_ROUTE_CMD)


@pytest.mark.parametrize("kind", ["one session", "checkpoints", "stages"])
@pytest.mark.parametrize(
    "repairs",
    [
        {},
        {
            "global_route_repair": {"setup-margin": 0.1, "hold-margin": 0},
            "post_route_repair": {"setup-margin": 0, "reroute": "eco"},
        },
        {
            "global_route_repair": {"setup-margin": 0, "reroute": "full"},
            "post_route_repair": {"setup-margin": 0, "reroute": "full"},
        },
        {"global_route_hold_repair": True},
    ],
)
def test_a_flow_without_options_is_the_template_as_before(
    tmp_path, monkeypatch, kind, repairs
):
    """With no `global-route:` key set, every script a run writes, post-route rounds included, is the earlier template's render, whole."""
    platform = _gr_platform(tmp_path, **repairs)

    def _write(base: bool) -> dict[str, str]:
        pnr_cfg = _pnr_cfg(
            tmp_path,
            CHECKPOINT_STAGES if kind == "checkpoints" else None,
            stages=kind == "stages",
        )
        pnr_cfg.resolve_synth_cfg = MagicMock(
            return_value=MagicMock(get_top=lambda: "demo_top", get_name=lambda: "s")
        )
        backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), MagicMock())
        backend._ps_per_unit = 1.0
        run_dir = str(tmp_path / "ck" / "run")
        if kind != "one session" or "post_route_repair" in repairs:
            backend._ckpt_run_dir = run_dir
            os.makedirs(run_dir, exist_ok=True)
        if base:
            template = _base_template(backend._load_template())
            monkeypatch.setattr(backend, "_load_template", lambda: template)
        script = Path(backend._write_script(platform, pnr_cfg.get_floorplan()))
        written = {"pnr.tcl": script.read_text()}
        if backend._ckpt_run_dir:
            for path in sorted(Path(run_dir).glob("*.tcl")):
                written[path.name] = path.read_text()
        if backend._post_route is not None:
            written["round"] = backend._post_route_repair_script(1, run_dir)
        return written

    assert _write(base=False) == _write(base=True)


def test_every_full_route_takes_the_options_and_every_call_the_origin(tmp_path):
    platform = _gr_platform(
        tmp_path,
        _ALL,
        global_route_repair={
            "repair-design": {"slew-margin": 20},
            "setup-margin": 0,
            "reroute": "full",
        },
    )
    text = _render(tmp_path, platform)
    stage = text[text.index('puts ">>> Global route"') : text.index(">>> Detail route")]
    # The first route and the full reroute after repair.
    assert stage.count(_FULL) == 2
    assert len(re.findall(r"^global_route ", stage, re.MULTILINE)) == 4
    # The legalization between the repairs is incremental: origin and allow-congestion only.
    assert stage.count(_START) == 1
    assert stage.count(_END) == 1
    # The last route is a full one, so the grid already sits at the origin.
    assert _GUIDES not in text


def test_an_incremental_repair_carries_the_origin_and_rebuilds_the_grid(tmp_path):
    platform = _gr_platform(
        tmp_path,
        {"grid_origin": [3, 3], "allow_congestion": True},
        global_route_repair={"setup-margin": 0, "hold-margin": 0},
    )
    text = _render(tmp_path, platform)
    stage = text[text.index('puts ">>> Global route"') : text.index(">>> Detail route")]
    assert stage.count(_START) == 1
    assert stage.count(_END) == 1
    assert "-start_incremental\n" not in stage
    assert (
        stage.count("global_route -congestion_iterations 20 -grid_origin {3 3} ") == 1
    )
    # The guides round trip follows the last incremental route, before detailed route.
    guides = stage.index(_GUIDES)
    assert stage.rindex(_END) < guides
    assert "file delete $OUT_DIR/grid_origin.guide\n" in stage


def test_the_hold_shorthand_carries_the_origin(tmp_path):
    platform = _gr_platform(
        tmp_path, {"grid_origin": [3, 3]}, global_route_hold_repair=True
    )
    text = _render(tmp_path, platform)
    assert "global_route -start_incremental -grid_origin {3 3}\n" in text
    assert (
        "global_route -end_incremental -grid_origin {3 3} -congestion_report_file "
        "$CONGESTION_RPT -verbose\ncheck_placement -verbose\n" + 'puts ">>> Global'
    ) in text
    assert _GUIDES in text


def test_full_route_options_stay_off_incremental_calls(tmp_path):
    platform = _gr_platform(
        tmp_path,
        {
            "congestion_iterations": 100,
            "congestion_report_iter_step": 25,
            "critical_nets_percentage": 30,
            "resistance_aware": True,
        },
        global_route_repair={"setup-margin": 0},
    )
    text = _render(tmp_path, platform)
    for line in text.splitlines():
        if "_incremental" in line:
            assert line.split()[:2] == ["global_route", line.split()[1]]
            for option in (
                "-congestion_iterations",
                "-congestion_report_iter_step",
                "-critical_nets_percentage",
                "-resistance_aware",
                "-grid_origin",
            ):
                assert option not in line
    assert _GUIDES not in text


def test_per_layer_adjustments_follow_the_all_layers_one(tmp_path):
    platform = _gr_platform(
        tmp_path,
        {"layer_adjustment": {"metal2": 0.9, "metal3": 0.8}},
        routing_layer_adjustment=0.2,
    )
    text = _render(tmp_path, platform)
    assert (
        "set_global_routing_layer_adjustment $SIGNAL_LAYERS 0.2\n"
        "set_global_routing_layer_adjustment metal2 0.9\n"
        "set_global_routing_layer_adjustment metal3 0.8\n"
        "global_route -congestion_iterations 20 "
    ) in text


def test_per_layer_adjustments_apply_without_the_all_layers_one(tmp_path):
    text = _render(tmp_path, _gr_platform(tmp_path, {"layer_adjustment": {"m2": 0}}))
    assert "set_global_routing_layer_adjustment m2 0\nglobal_route " in text
    assert "$SIGNAL_LAYERS 0" not in text


@pytest.mark.parametrize("reroute", ["eco", "full"])
def test_post_route_rounds_carry_the_options(tmp_path, reroute):
    platform = _gr_platform(
        tmp_path, _ALL, post_route_repair={"setup-margin": 0, "reroute": reroute}
    )
    pnr_cfg = _pnr_cfg(tmp_path, None)
    pnr_cfg.resolve_synth_cfg = MagicMock(
        return_value=MagicMock(get_top=lambda: "demo_top", get_name=lambda: "s")
    )
    backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), MagicMock())
    backend._ps_per_unit = 1.0
    run_dir = tmp_path / "ck" / "run"
    run_dir.mkdir(parents=True)
    backend._ckpt_run_dir = str(run_dir)
    backend._write_script(platform, pnr_cfg.get_floorplan())
    round_ = backend._post_route_repair_script(1, str(run_dir))
    assert round_.count(_FULL) == (1 if reroute == "full" else 0)
    assert _START in round_ and _END in round_
    assert "global_route -start_incremental\n" not in round_
    # The round ends on an incremental route, so the grid is rebuilt before detailed route.
    assert (
        round_.rindex(_END)
        < round_.index(_GUIDES)
        < round_.index("\ndetailed_route \\")
    )


# -- digest -----------------------------------------------------------------------


def _digest_cfg(tmp_path, overrides=PnrRunOverrides()):
    return replace(_pnr_cfg(tmp_path, None), harden=True, overrides=overrides)


def test_unset_options_add_nothing_to_the_digest(tmp_path):
    config = pnr_abstract.abstract_config(_digest_cfg(tmp_path), _gr_platform(tmp_path))
    assert "global_route" not in config["routing"]
    empty = pnr_abstract.abstract_config(
        _digest_cfg(tmp_path), _gr_platform(tmp_path, {})
    )
    assert pnr_abstract.config_digest(empty) == pnr_abstract.config_digest(config)


@pytest.mark.parametrize(
    ("gr", "expected"),
    [
        (GlobalRoute(congestion_iterations=100), {"congestion-iterations": 100}),
        (GlobalRoute(grid_origin=(3, 3)), {"grid-origin": [3, 3]}),
        (
            GlobalRoute(layer_adjustment=(("metal2", 0.9),)),
            {"layer-adjustment": {"metal2": 0.9}},
        ),
        (GlobalRoute(resistance_aware=False), {"resistance-aware": False}),
        (
            GlobalRoute(critical_nets_percentage=30.0),
            {"critical-nets-percentage": 30.0},
        ),
    ],
)
def test_a_set_option_joins_the_digest(tmp_path, gr, expected):
    platform = _gr_platform(tmp_path)
    plain = pnr_abstract.abstract_config(_digest_cfg(tmp_path), platform)
    over = pnr_abstract.abstract_config(
        _digest_cfg(tmp_path, PnrRunOverrides(global_route=gr)), platform
    )
    assert over["routing"]["global_route"] == expected
    assert pnr_abstract.config_digest(over) != pnr_abstract.config_digest(plain)


def test_a_platform_option_joins_the_digest(tmp_path):
    config = pnr_abstract.abstract_config(
        _digest_cfg(tmp_path), _gr_platform(tmp_path, {"congestion_iterations": 50})
    )
    assert config["routing"]["global_route"] == {"congestion-iterations": 50}


# -- a failed global route keeps its database ------------------------------------


def _failing(tmp_path, monkeypatch, **kw):
    return _backend(tmp_path, monkeypatch, env={"RB_STUB_FAIL": "global_route"}, **kw)


def _assert_kept(artefacts: Path, run_dir: Path, res) -> None:
    assert res.results["result"] == "FAIL"
    run_id = run_dir.name
    assert res.results["failed_global_route"] == f"{run_id}/global_route"
    row = RtlBuddy._pnr_result_row(None, {"pnr_name": "r", "results": res})
    assert row["failed_global_route"] == f"{run_id}/global_route"
    for ext in ("odb", "def", "sdc"):
        assert (run_dir / f"04_global_route.{ext}").is_file()
    assert not (artefacts / "global_route_failed").exists()
    event = [
        e
        for e in _events(run_dir)
        if e.get("event") == "checkpoint" and e.get("stage") == "global_route"
    ][-1]
    assert event["failed"] is True
    assert event["files"]["def"] == "04_global_route.def"
    # `rb pnr-export --checkpoint` opens it, labelled failed; no resume starts from it.
    for spec in ("global_route", f"{run_id}/global_route"):
        ref = pnr_checkpoints.resolve_checkpoint(str(artefacts), spec)
        assert not isinstance(ref, str), ref
        assert ref.failed is True
        assert ref.def_path == str(run_dir / "04_global_route.def")
        assert ref.provenance(None)["failed"] is True
    assert pnr_checkpoints.written_checkpoint(str(run_dir), "global_route") is None


def test_a_run_without_checkpoints_keeps_the_failed_database(tmp_path, monkeypatch):
    backend, artefacts = _failing(tmp_path, monkeypatch, checkpoints=None)

    res = backend.run()

    runs = [
        p
        for p in (artefacts / "checkpoints").iterdir()
        if p.is_dir() and not p.is_symlink()
    ]
    assert len(runs) == 1
    assert (artefacts / "checkpoints" / "latest").resolve() == runs[0].resolve()
    _assert_kept(artefacts, runs[0], res)
    assert res.results["checkpoint_dir"] == str(runs[0])
    assert res.results["checkpoint_stages"] == ["global_route"]


@pytest.mark.parametrize("stages", [False, True])
def test_a_checkpointed_run_files_the_failed_database_with_its_checkpoints(
    tmp_path, monkeypatch, stages
):
    backend, artefacts = _failing(tmp_path, monkeypatch, stages=stages)

    res = backend.run()

    run_dir = Path(backend._ckpt_run_dir)
    _assert_kept(artefacts, run_dir, res)
    manifest = pnr_checkpoints.read_manifest(str(run_dir))
    assert manifest["checkpoints"]["global_route"]["failed"] is True
    assert "failed" not in manifest["checkpoints"]["cts"]


def test_a_failure_elsewhere_files_nothing(tmp_path, monkeypatch):
    backend, artefacts = _backend(
        tmp_path, monkeypatch, env={"RB_STUB_FAIL": "detailed_route"}
    )

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert "failed_global_route" not in res.results
    run_dir = backend._ckpt_run_dir
    assert pnr_checkpoints.written_checkpoint(run_dir, "global_route") is not None
    assert not any(e.get("failed") for e in _events(run_dir))


def test_the_congestion_report_is_copied_into_the_checkpoint(tmp_path):
    artefacts = tmp_path / "a"
    source = artefacts / pnr_checkpoints.FAILED_GLOBAL_ROUTE_DIRNAME
    source.mkdir(parents=True)
    for ext in ("odb", "def", "sdc"):
        (source / f"global_route.{ext}").write_text(ext)
    (artefacts / "congestion.rpt").write_text("overflow")
    run_dir = artefacts / "checkpoints" / "run"

    files = pnr_checkpoints.keep_failed_global_route(
        str(artefacts), str(run_dir), design="top"
    )

    assert files == {
        "odb": "04_global_route.odb",
        "def": "04_global_route.def",
        "sdc": "04_global_route.sdc",
        "congestion": "04_global_route.congestion.rpt",
    }
    assert (run_dir / "04_global_route.congestion.rpt").read_text() == "overflow"
    assert (artefacts / "congestion.rpt").is_file()
    assert not source.exists()


def test_each_corner_sdc_mode_is_filed_with_the_checkpoint(tmp_path):
    artefacts = tmp_path / "a"
    source = artefacts / pnr_checkpoints.FAILED_GLOBAL_ROUTE_DIRNAME
    source.mkdir(parents=True)
    for name in ("odb", "def", "sdc", "tt.sdc", "ss.sdc"):
        (source / f"global_route.{name}").write_text(name)
    run_dir = artefacts / "checkpoints" / "run"

    files = pnr_checkpoints.keep_failed_global_route(
        str(artefacts), str(run_dir), design="top"
    )

    assert files["sdc"] == "04_global_route.sdc"
    assert files["sdc_tt"] == "04_global_route.tt.sdc"
    assert files["sdc_ss"] == "04_global_route.ss.sdc"
    assert (run_dir / "04_global_route.ss.sdc").read_text() == "ss.sdc"


def test_the_keeper_writes_the_sdc_as_a_checkpoint_does(tmp_path):
    text = _render(tmp_path, _gr_platform(tmp_path))
    assert (
        "    write_sdc -no_timestamp -digits 9 [file join $dir global_route.sdc]\n"
        in text
    )


def test_nothing_is_filed_without_a_failed_database(tmp_path):
    assert (
        pnr_checkpoints.keep_failed_global_route(
            str(tmp_path), str(tmp_path / "run"), design="top"
        )
        is None
    )
    assert not (tmp_path / "run").exists()


def test_a_stale_failed_database_is_cleared_before_a_run(tmp_path, monkeypatch):
    backend, artefacts = _backend(tmp_path, monkeypatch, checkpoints=None)
    stale = artefacts / pnr_checkpoints.FAILED_GLOBAL_ROUTE_DIRNAME
    stale.mkdir(parents=True)
    (stale / "global_route.odb").write_text("old")

    res = backend.run()

    assert res.results["result"] == "PASS"
    assert not stale.exists()


def test_the_flow_installs_the_keeper_before_the_first_route(tmp_path):
    text = _render(tmp_path, _gr_platform(tmp_path))
    keeper = text.index(
        "trace add execution global_route leave rb::keep_failed_global_route\n"
    )
    assert text.index("proc rb::keep_failed_global_route") < keeper
    assert keeper < text.index(_GLOBAL_ROUTE_CMD)


# -- allow-congestion is never a clean pass ---------------------------------------


def _allow_backend(tmp_path, monkeypatch, *, harden=False, **kw):
    pnr_cfg = replace(
        _pnr_cfg(tmp_path, None),
        harden=harden,
        overrides=PnrRunOverrides(global_route=GlobalRoute(allow_congestion=True)),
    )
    return _backend(tmp_path, monkeypatch, pnr_cfg=pnr_cfg, **kw)


_USAGE = (
    "[INFO GRT-0096] Final congestion report:\n"
    "Layer         Resource        Demand        Usage (%)    Max H / Max V / Total Overflow\n"
    "---------------------------------------------------------------------------------------\n"
    "Total             1000           900          90.00%             1 /  2 /  7\n"
)


def _with_usage_table(monkeypatch):
    real = OpenRoadPnr._parse_gr_overflow

    def _parse(self, log_text):
        return real(self, log_text + _USAGE)

    monkeypatch.setattr(OpenRoadPnr, "_parse_gr_overflow", _parse)


def test_an_allow_congestion_run_passes_qualified(tmp_path, monkeypatch):
    _with_usage_table(monkeypatch)
    backend, _artefacts = _allow_backend(tmp_path, monkeypatch)

    res = backend.run()

    assert res.results["result"] == "PASS"
    assert res.results["allow_congestion"] is True
    assert res.results["global_route_overflow"] == 7
    assert (
        "global-route.allow-congestion (diagnosis only): global route overflow 7"
        in res.results["desc"]
    )
    row = RtlBuddy._pnr_result_row(None, {"pnr_name": "r", "results": res})
    assert row["allow_congestion"] is True
    assert row["global_route_overflow"] == 7


def test_an_allow_congestion_run_without_overflow_is_still_qualified(
    tmp_path, monkeypatch
):
    backend, _artefacts = _allow_backend(tmp_path, monkeypatch)

    res = backend.run()

    assert res.results["result"] == "PASS"
    assert res.results["desc"].startswith("P&R passed; ")
    assert "global-route.allow-congestion (diagnosis only)" in res.results["desc"]


def test_a_stopped_allow_congestion_run_is_qualified(tmp_path, monkeypatch):
    _with_usage_table(monkeypatch)
    backend, _artefacts = _allow_backend(
        tmp_path, monkeypatch, stop_after="global_route"
    )

    res = backend.run()

    assert res.results["result"] == "PASS"
    assert res.results["allow_congestion"] is True
    assert res.results["desc"].startswith("P&R stopped after global_route")
    assert (
        "allow-congestion (diagnosis only): global route overflow 7"
        in (res.results["desc"])
    )


def test_a_run_without_allow_congestion_carries_no_qualifier(tmp_path, monkeypatch):
    backend, _artefacts = _backend(tmp_path, monkeypatch, checkpoints=None)

    res = backend.run()

    assert res.results["result"] == "PASS"
    assert "allow_congestion" not in res.results
    assert "allow-congestion" not in (res.results["desc"] or "")


def test_allow_congestion_cannot_harden(tmp_path, monkeypatch):
    backend, _artefacts = _allow_backend(tmp_path, monkeypatch, harden=True)

    res = backend.run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "setup"
    assert "cannot be harden: true" in res.results["desc"]
