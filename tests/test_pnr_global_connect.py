"""Tests that the flow reapplies the platform's global connections after it adds cells.

Buffers and fill cells added after `pdngen` carry no supply connections until the rules that `add_global_connection` stored in the database are applied again. The generated scripts run under the stub OpenROAD of `test_pnr_checkpoints`, which logs `stub: global_connect` and each database write.
"""

from dataclasses import replace

import pytest
from test_pnr_checkpoints import _backend, _pdk, _pnr_cfg, _runs
from test_pnr_repair import _PR, _hook, _platform, _repair_backend, _with_repair

from rtl_buddy.config.pnr import CHECKPOINT_STAGES
from rtl_buddy.tools import pnr_checkpoints, pnr_openroad

_RULES = {"RB_STUB_RULES": "1"}
_CONNECT = "stub: global_connect"


def _log(artefacts) -> list[str]:
    return (artefacts / "pnr.log").read_text().splitlines()


def _after(lines: list[str], marker: str) -> list[str]:
    return [lines[i + 1] for i, line in enumerate(lines[:-1]) if line == marker]


#: A post-route repair block whose one round repairs: setup misses its margin.
_ROUND = {"post_route": {**_PR, "rounds": 1}}
_ROUND_ENV = {**_RULES, "RB_STUB_SETUP": "-0.2"}


def _run_backend(tmp_path, monkeypatch, *, post_route: bool, **kw):
    """A stub-harness backend with the rules stored, and with a repairing post-route round when `post_route`."""
    if post_route:
        return _repair_backend(tmp_path, monkeypatch, env=_ROUND_ENV, **_ROUND, **kw)
    return _backend(tmp_path, monkeypatch, env=_RULES, **kw)


def _checkpoint_stages(backend) -> list[str]:
    """The stages of the run `backend` ran that end on a checkpoint: all but `finish`."""
    return [stage for stage in backend._stages if stage != "finish"]


@pytest.mark.parametrize("post_route", [False, True])
@pytest.mark.parametrize("stages", [False, True])
def test_every_checkpoint_is_written_after_the_connections_are_made(
    tmp_path, monkeypatch, stages, post_route
):
    backend, artefacts = _run_backend(
        tmp_path, monkeypatch, stages=stages, post_route=post_route
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    lines = _log(artefacts)
    stages_run = _checkpoint_stages(backend)
    assert ("post_route_repair" in stages_run) == post_route
    for stage in stages_run:
        name = f"{pnr_checkpoints.STAGE_INDEX[stage]}_{stage}"
        (following,) = _after(lines, f">>> Checkpoint {name}")
        assert following == _CONNECT
        assert lines.index(f"stub: write_db {name}.odb") > lines.index(
            f">>> Checkpoint {name}"
        )


@pytest.mark.parametrize("stages", [False, True])
def test_the_final_outputs_are_written_after_the_connections_are_made(
    tmp_path, monkeypatch, stages
):
    backend, artefacts = _backend(tmp_path, monkeypatch, stages=stages, env=_RULES)

    backend.run()

    lines = _log(artefacts)
    outputs = lines.index(">>> Write outputs")
    assert lines[outputs - 1] == _CONNECT
    assert outputs > lines.index(">>> Fill insertion")
    assert lines.index("stub: write_def demo_top.def") > outputs
    assert lines.index("stub: write_db demo_top.routed.odb") > outputs


#: The flow step that opens each stage after the first, which the connections at the end of the stage before it precede.
_OPENERS = (
    ">>> Global placement",
    ">>> Clock tree synthesis",
    ">>> Global route",
    ">>> Detail route",
    ">>> Fill insertion",
)


@pytest.mark.parametrize("checkpoints", [None, CHECKPOINT_STAGES])
def test_every_stage_of_a_one_session_run_ends_with_the_connections_made(
    tmp_path, monkeypatch, checkpoints
):
    """A run behaves the same with or without checkpoints: the connections do not wait for a checkpoint."""
    backend, artefacts = _backend(
        tmp_path, monkeypatch, checkpoints=checkpoints, env=_RULES
    )

    assert backend.run().results["result"] == "PASS"

    lines = _log(artefacts)
    for opener in _OPENERS:
        at = lines.index(opener)
        last = max(i for i, line in enumerate(lines[:at]) if line == _CONNECT)
        # Only a checkpoint write may sit between the connections and the next stage.
        assert all(
            line.startswith((">>> Checkpoint", "stub: write_"))
            for line in lines[last + 1 : at]
        ), opener
    assert lines[lines.index(">>> Write outputs") - 1] == _CONNECT


def test_a_run_without_checkpoints_connects_at_every_stage_end_and_after_fill(
    tmp_path, monkeypatch
):
    backend, artefacts = _backend(tmp_path, monkeypatch, checkpoints=None, env=_RULES)

    assert backend.run().results["result"] == "PASS"

    lines = _log(artefacts)
    # Five stage ends, after any before-detailed-route hook, after fill, and before the outputs.
    assert lines.count(_CONNECT) == 8
    connects = [i for i, line in enumerate(lines) if line == _CONNECT]
    assert connects[6] > lines.index(">>> Fill insertion")


@pytest.mark.parametrize("post_route", [False, True])
def test_every_staged_session_connects_before_it_writes_its_checkpoint(
    tmp_path, monkeypatch, post_route
):
    backend, artefacts = _run_backend(
        tmp_path, monkeypatch, stages=True, post_route=post_route
    )

    assert backend.run().results["result"] == "PASS"

    (run_dir,) = _runs(artefacts)
    for stage in _checkpoint_stages(backend):
        end = f"rb::ckpt::stage_end {stage}"
        scripts = [
            text
            for path in pnr_openroad.stage_scripts(str(run_dir), stage)
            if end in (text := open(path).read())
        ]
        assert scripts, stage
        for script in scripts:
            assert script.index("\nrb::global_connect\n") < script.index(end)


@pytest.mark.parametrize("stages", [False, True])
def test_the_global_route_stage_connects_the_cells_its_repair_added(
    tmp_path, monkeypatch, stages
):
    backend, artefacts = _backend(tmp_path, monkeypatch, stages=stages, env=_RULES)
    backend.root_cfg.get_pnr_platform_cfg.return_value = _platform(
        _pdk(tmp_path), global_route_hold_repair=True
    )

    assert backend.run().results["result"] == "PASS"

    (run_dir,) = _runs(artefacts)
    script = (
        (run_dir / "04_global_route.tcl") if stages else (artefacts / "pnr.tcl")
    ).read_text()
    tail = script[script.index(">>> Post-global-route hold repair") :]
    connect = tail.index("\nrb::global_connect\n")
    assert tail.rindex("\nglobal_route -end_incremental ", 0, connect) > 0
    assert connect < tail.index("rb::ckpt::stage_end global_route")


@pytest.mark.parametrize("reroute", ["eco", "full"])
@pytest.mark.parametrize("stages", [False, True])
def test_a_post_route_repair_round_connects_the_cells_it_added_before_its_hooks(
    tmp_path, monkeypatch, stages, reroute
):
    cfg = replace(
        _pnr_cfg(tmp_path, stages=stages),
        hooks={
            "before-detailed-route": _hook(
                tmp_path, "bdr", 'puts "MARK before-detailed-route"\n'
            )
        },
    )
    backend, artefacts = _backend(
        tmp_path, monkeypatch, pnr_cfg=cfg, stages=stages, env=_ROUND_ENV
    )
    _with_repair(
        backend, tmp_path, post_route={**_ROUND["post_route"], "reroute": reroute}
    )

    assert backend.run().results["result"] == "PASS"

    (run_dir,) = _runs(artefacts)
    script = (run_dir / "06_post_route_repair.r1.repair.tcl").read_text()
    hook = script.index("\nrb::hook::run before-detailed-route ")
    route = script.index("\ndetailed_route \\")
    legalized = max(
        script.rindex("\ncheck_placement -verbose\n", 0, hook),
        script.rindex("\nglobal_route -end_incremental ", 0, hook),
    )
    connects = [
        i
        for i in range(len(script))
        if script.startswith("\nrb::global_connect\n", i) and legalized < i < route
    ]
    assert len(connects) == 2
    assert connects[0] < hook < connects[1]
    log = (run_dir / "06_post_route_repair.r1.repair.log").read_text().splitlines()
    marked = log.index("MARK before-detailed-route")
    assert log[marked - 1].startswith(">>> Hook before-detailed-route: ")
    assert log[marked - 2] == _CONNECT
    assert log[marked + 1 : marked + 3] == [
        _CONNECT,
        ">>> Post-route repair round 1: detailed route",
    ]


@pytest.mark.parametrize("stages", [False, True])
def test_detailed_route_starts_after_the_connections_of_cells_a_hook_added(
    tmp_path, monkeypatch, stages
):
    hook = tmp_path / "hooks" / "before-detailed-route.tcl"
    hook.parent.mkdir()
    hook.write_text('puts "MARK before-detailed-route"\n')
    cfg = replace(
        _pnr_cfg(tmp_path, stages=stages),
        hooks={"before-detailed-route": (str(hook),)},
    )
    backend, artefacts = _backend(tmp_path, monkeypatch, pnr_cfg=cfg, env=_RULES)

    assert backend.run().results["result"] == "PASS"

    lines = _log(artefacts)
    marked = lines.index("MARK before-detailed-route")
    assert lines[marked + 1 : marked + 3] == [_CONNECT, ">>> Detail route"]


@pytest.mark.parametrize("stages", [False, True])
def test_extraction_sees_the_connected_design(tmp_path, monkeypatch, stages):
    (tmp_path / "pdk").mkdir()
    (tmp_path / "pdk" / "rcx.rules").write_text("rules\n")
    backend, artefacts = _backend(
        tmp_path,
        monkeypatch,
        stages=stages,
        env=_RULES,
        pdk_fields={"rcx_rules": "pdk/rcx.rules"},
    )

    assert backend.run().results["result"] == "PASS"

    lines = _log(artefacts)
    extraction = lines.index(">>> Parasitic extraction (OpenRCX)")
    assert lines[extraction - 1] == _CONNECT
    assert extraction > lines.index(">>> Fill insertion")


def test_a_stopped_run_connects_before_its_checkpoint(tmp_path, monkeypatch):
    backend, artefacts = _backend(
        tmp_path, monkeypatch, checkpoints=None, stop_after="cts", env=_RULES
    )

    assert backend.run().results["result"] == "PASS"

    lines = _log(artefacts)
    assert _after(lines, ">>> Checkpoint 03_cts") == [_CONNECT]
    assert ">>> Write outputs" not in lines


def test_the_staged_finish_session_connects_after_fill(tmp_path, monkeypatch):
    backend, artefacts = _backend(tmp_path, monkeypatch, stages=True, env=_RULES)

    backend.run()

    (run_dir,) = _runs(artefacts)
    finish = (run_dir / "07_finish.log").read_text().splitlines()
    assert finish.index(_CONNECT) > finish.index(">>> Fill insertion")
    assert finish.index(_CONNECT) < finish.index("stub: write_db demo_top.routed.odb")


@pytest.mark.parametrize("stages", [False, True])
def test_a_database_without_rules_is_left_alone(tmp_path, monkeypatch, stages):
    backend, artefacts = _backend(tmp_path, monkeypatch, stages=stages)

    assert backend.run().results["result"] == "PASS"

    assert _CONNECT not in _log(artefacts)


def test_the_flow_defines_a_guarded_connect_before_any_stage():
    from importlib.resources import files

    text = files("rtl_buddy.pnr").joinpath("flow.tcl.template").read_text()
    head = text[: text.index("{{ @load }}")]

    assert "proc rb::global_connect" in head
    assert "getGlobalConnects" in head
    # The end of five stages, before detailed route, after fill, and before the outputs.
    assert text.count("rb::global_connect\n") == 8
    assert text.rindex("rb::global_connect\n") > text.index("filler_placement")
