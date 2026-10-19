"""Tests that the flow reapplies the platform's global connections after it adds cells.

Buffers and fill cells added after `pdngen` carry no supply connections until the rules that `add_global_connection` stored in the database are applied again. The generated scripts run under the stub OpenROAD of `test_pnr_checkpoints`, which logs `stub: global_connect` and each database write.
"""

import pytest
from test_pnr_checkpoints import _backend, _runs

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


def test_a_run_without_checkpoints_connects_once_before_its_outputs(
    tmp_path, monkeypatch
):
    backend, artefacts = _backend(tmp_path, monkeypatch, checkpoints=None, env=_RULES)

    assert backend.run().results["result"] == "PASS"

    lines = _log(artefacts)
    assert lines.count(_CONNECT) == 1
    assert lines[lines.index(">>> Write outputs") - 1] == _CONNECT


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


def test_the_flow_defines_one_guarded_connect_before_any_stage():
    from importlib.resources import files

    text = files("rtl_buddy.pnr").joinpath("flow.tcl.template").read_text()
    head = text[: text.index("{{ @load }}")]

    assert "proc rb::global_connect" in head
    assert "getGlobalConnects" in head
    assert text.count("\nrb::global_connect\n") == 1
    assert text.index("\nrb::global_connect\n") > text.index("filler_placement")
