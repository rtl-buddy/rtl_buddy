"""Tests that the flow reapplies the platform's global connections after it adds cells.

Buffers and fill cells added after `pdngen` carry no supply connections until the rules that `add_global_connection` stored in the database are applied again. The generated scripts run under the stub OpenROAD of `test_pnr_checkpoints`, which logs `stub: global_connect` and each database write.
"""

from dataclasses import replace

import pytest
from test_pnr_checkpoints import _backend, _pnr_cfg, _runs

from rtl_buddy.config.pnr import CHECKPOINT_STAGES
from rtl_buddy.tools import pnr_checkpoints

_RULES = {"RB_STUB_RULES": "1"}
_CONNECT = "stub: global_connect"


def _log(artefacts) -> list[str]:
    return (artefacts / "pnr.log").read_text().splitlines()


def _after(lines: list[str], marker: str) -> list[str]:
    return [lines[i + 1] for i, line in enumerate(lines[:-1]) if line == marker]


@pytest.mark.parametrize("stages", [False, True])
def test_every_checkpoint_is_written_after_the_connections_are_made(
    tmp_path, monkeypatch, stages
):
    backend, artefacts = _backend(tmp_path, monkeypatch, stages=stages, env=_RULES)

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    lines = _log(artefacts)
    for stage in CHECKPOINT_STAGES:
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


def test_every_staged_session_connects_before_it_writes_its_checkpoint(
    tmp_path, monkeypatch
):
    backend, artefacts = _backend(tmp_path, monkeypatch, stages=True, env=_RULES)

    assert backend.run().results["result"] == "PASS"

    (run_dir,) = _runs(artefacts)
    for stage in CHECKPOINT_STAGES:
        name = f"{pnr_checkpoints.STAGE_INDEX[stage]}_{stage}"
        script = (run_dir / f"{name}.tcl").read_text()
        assert script.index("\nrb::global_connect\n") < script.index(
            f"rb::ckpt::stage_end {stage}"
        )


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
