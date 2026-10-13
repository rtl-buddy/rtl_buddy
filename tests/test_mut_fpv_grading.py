"""FPV-oracle grading of mutants (rtl-buddy/rtl_buddy#804).

Each mutant's proof is a real ``SbyFpv.run()`` over a captured sby workdir from ``tests/fixtures/sby_logs``, picked by a
marker in the mutant source. A proof that ended without a verdict (``fail_stage``) is errored; a disproof, including a
failed induction step in ``mode prove``, is a kill.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rtl_buddy.runner.fpv_results import FpvPassResults
from rtl_buddy.runner.mut_results import ERRORED, KILLED, SURVIVED

from test_fpv import _tool_cfg
from test_fpv_induction_unknown import _RC, FIXTURES
from test_mut import (
    _DESIGN_SV,
    _install_stub_xeno,
    _Mutant,
    _MutationKind,
    _Prediction,
    _runner,
    _write_project,
)

# Marker in the mutant source -> captured sby workdir its proof "produces".
_MARKERS = {
    "SBY_ERROR": "prove_error",
    "SBY_UNKNOWN": "prove_unknown_no_induction",
    "SBY_BASECASE_FAIL": "prove_basecase_fail",
    "SBY_INDUCTION": "prove_induction_unknown",
}


class _FixtureFpvRunner:
    """Stands in for FpvRunner: the baseline passes, a marked mutant replays its sby fixture through SbyFpv.run()."""

    def __init__(self, name, root_cfg, fpv_cfg, suite_dir):
        self.fpv_cfg = fpv_cfg
        self.suite_dir = suite_dir

    def run(self):
        from rtl_buddy.tools.sby_fpv import SbyFpv

        model_dir = Path(os.path.dirname(self.fpv_cfg.get_model().path))
        sv = next(model_dir.glob("*.sv")).read_text()
        case = next((c for m, c in _MARKERS.items() if m in sv), None)
        if case is None:
            return FpvPassResults(name="x", mode="prove", depth=4)

        cfg = self.fpv_cfg
        cfg.mode = "prove"
        cfg.vacuity = False
        cfg.coi = False
        sby = SbyFpv(
            name="t", fpv_cfg=cfg, tool_cfg=_tool_cfg(), suite_dir=self.suite_dir
        )
        fl = Path(sby._filelist_path())
        fl.write_text(f"{model_dir / 'leaf.sv'}\n")
        sby._write_filelist = lambda: str(fl)
        sby._probe_sby_version = lambda exe: None

        def fake_run(cmd, log_path):
            workdir = Path(sby._workdir_path())
            shutil.copytree(FIXTURES / case, workdir)
            Path(log_path).write_text((workdir / "logfile.txt").read_text())
            return SimpleNamespace(returncode=_RC[case])

        sby._run = fake_run
        return sby.run()


def _mutant(marker: str) -> _Mutant:
    return _Mutant(
        sv=f"// {marker}\n" + _DESIGN_SV,
        diff_summary=marker,
        seed=1,
        prediction=_Prediction(),
        kind=_MutationKind.ARITH_FLIP,
    )


@pytest.mark.parametrize(
    "marker,outcome,verdict",
    [
        ("SBY_ERROR", ERRORED, "fpv=ERROR"),
        ("SBY_UNKNOWN", ERRORED, "fpv=ERROR"),
        ("SBY_BASECASE_FAIL", KILLED, "fpv=FAIL"),
        ("SBY_INDUCTION", KILLED, "fpv=FAIL"),
        ("survive", SURVIVED, "fpv=PASS"),
    ],
)
def test_fpv_oracle_grades_a_mutant_by_its_proof(
    tmp_path, monkeypatch, marker, outcome, verdict
):
    _install_stub_xeno(monkeypatch, [_mutant(marker)])
    runner = _runner(tmp_path, _write_project(tmp_path))
    with patch("rtl_buddy.runner.mut_runner.FpvRunner", _FixtureFpvRunner):
        results = runner.run()
    assert results.baseline_verdict == "fpv=PASS"
    (o,) = results.outcomes
    assert (o.outcome, o.verdict) == (outcome, verdict)


def test_a_tool_failure_is_left_out_of_the_score(tmp_path, monkeypatch):
    _install_stub_xeno(
        monkeypatch, [_mutant("SBY_ERROR"), _mutant("SBY_INDUCTION"), _mutant("x")]
    )
    runner = _runner(tmp_path, _write_project(tmp_path))
    with patch("rtl_buddy.runner.mut_runner.FpvRunner", _FixtureFpvRunner):
        results = runner.run()
    assert (results.killed(), results.survived(), results.errored()) == (1, 1, 1)
    assert results.score() == pytest.approx(0.5)
