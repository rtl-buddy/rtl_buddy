"""Tests for the ``rb mut`` slice: mut.yaml config, the missing-xeno
guard, and the MutRunner orchestration (candidate listing, mutant
materialisation, FPV-verdict -> outcome classification, scoring).

The external ``rtl-buddy-xeno`` engine is replaced with an in-process
stub so the orchestration is exercised without the Verible / pyslang
toolchain. The FPV proof is faked by patching ``FpvRunner`` to read the
spliced design file and return a verdict driven by markers in the
mutant source.
"""

from __future__ import annotations

import enum
import os
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

from rtl_buddy.config.mut import MutSuiteConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.runner.mut_results import ERRORED, KILLED, SURVIVED, MutResults
from rtl_buddy.runner.fpv_results import FpvFailResults, FpvPassResults


# ---------------------------------------------------------------------------
# Project fixture: a leaf design + models.yaml + fpv.yaml + mut.yaml
# ---------------------------------------------------------------------------

_DESIGN_SV = dedent(
    """\
    module leaf (input logic clk, input logic en, output logic [2:0] cnt);
      always_ff @(posedge clk) if (en) cnt <= cnt + 1'b1;
    endmodule
    """
)


def _write_project(root: Path) -> Path:
    design_dir = root / "design" / "leaf"
    design_dir.mkdir(parents=True)
    (design_dir / "leaf.sv").write_text(_DESIGN_SV)
    (design_dir / "models.yaml").write_text(
        dedent(
            """\
            rtl-buddy-filetype: model_config
            models:
              - name: "leaf"
                filelist: ["-v leaf.sv"]
            """
        )
    )

    fpv_dir = root / "fpv" / "leaf"
    fpv_dir.mkdir(parents=True)
    (fpv_dir / "fpv.yaml").write_text(
        dedent(
            """\
            rtl-buddy-filetype: fpv_config
            verifications:
              - name: "leaf_safety"
                desc: "safety"
                tool: "sby"
                model: "leaf"
                model_path: "../../design/leaf/models.yaml"
                top: "leaf"
                properties: []
                mode: "bmc"
                depth: 16
            """
        )
    )

    mut_path = fpv_dir / "mut.yaml"
    mut_path.write_text(
        dedent(
            """\
            rtl-buddy-filetype: mut_config
            model: "leaf"
            model_path: "../../design/leaf/models.yaml"
            design_file: "../../design/leaf/leaf.sv"
            operators: [arith_flip, cond_const, assign_drop]
            verify:
              fpv_config: "fpv.yaml"
              verification: "leaf_safety"
            budget:
              max_mutants: 10
              schedule: round_robin
            """
        )
    )
    return mut_path


# ---------------------------------------------------------------------------
# Stub rtl-buddy-xeno
# ---------------------------------------------------------------------------


class _MutationKind(enum.StrEnum):
    ARITH_FLIP = "arith_flip"
    BIT_OP_FLIP = "bit_op_flip"
    COND_NEGATE = "cond_negate"
    COND_CONST = "cond_const"
    ASSIGN_DROP = "assign_drop"
    PORT_BINDING_SWAP = "port_binding_swap"


class _Schedule(enum.StrEnum):
    SEQUENTIAL = "sequential"
    ROUND_ROBIN = "round_robin"


@dataclass(frozen=True)
class _Prediction:
    rationale: str = "stub"
    perturbs_signals: frozenset = field(default_factory=frozenset)
    perturbs_liveness: bool = False


@dataclass(frozen=True)
class _Mutant:
    sv: str
    diff_summary: str
    seed: int
    prediction: _Prediction
    kind: _MutationKind


@dataclass(frozen=True)
class _Site:
    kind: _MutationKind
    line: int
    column: int
    snippet: str
    prediction: _Prediction


# Three mutants: one killed (FAIL marker), one survived (PASS) with a
# prediction (-> predicted-observable miss), one errored (build break).
_STUB_MUTANTS = [
    _Mutant(
        sv="// KILL\n" + _DESIGN_SV,
        diff_summary="+ -> -",
        seed=1,
        prediction=_Prediction(perturbs_signals=frozenset({"cnt"})),
        kind=_MutationKind.ARITH_FLIP,
    ),
    _Mutant(
        sv="// survive\n" + _DESIGN_SV,
        diff_summary="cond -> 1",
        seed=2,
        prediction=_Prediction(perturbs_signals=frozenset({"cnt"})),
        kind=_MutationKind.COND_CONST,
    ),
    _Mutant(
        sv="// ERR\n" + _DESIGN_SV,
        diff_summary="drop assign",
        seed=3,
        prediction=_Prediction(),
        kind=_MutationKind.ASSIGN_DROP,
    ),
]

_STUB_SITES = [
    _Site(_MutationKind.ARITH_FLIP, 2, 40, "+", _Prediction()),
    _Site(_MutationKind.COND_CONST, 2, 22, "en", _Prediction()),
]


class _Mutator:
    def __init__(self, parent_sv: str):
        self.parent_sv = parent_sv

    @classmethod
    def from_sv(cls, path):
        return cls(Path(path).read_text())

    def generate(self, kinds, count, seed=0, schedule=None):
        yield from _STUB_MUTANTS[:count]

    def candidates(self, kinds):
        yield from _STUB_SITES


@pytest.fixture
def stub_xeno(monkeypatch):
    mod = types.ModuleType("rtl_buddy_xeno")
    mod.Mutator = _Mutator
    mod.MutationKind = _MutationKind
    mod.Schedule = _Schedule
    mod.Mutant = _Mutant
    mod.Site = _Site
    mod.Prediction = _Prediction
    monkeypatch.setitem(sys.modules, "rtl_buddy_xeno", mod)
    return mod


# A fake FpvRunner: reads the spliced .sv next to the model and maps a
# marker comment to a verdict. Baseline (unmutated) source has no marker
# -> PASS.
class _FakeFpvRunner:
    def __init__(self, name, root_cfg, fpv_cfg, suite_dir):
        self.fpv_cfg = fpv_cfg

    def run(self):
        model = self.fpv_cfg.get_model()
        design_dir = Path(os.path.dirname(model.path))
        sv = next(design_dir.glob("*.sv")).read_text()
        if "ERR" in sv:
            raise FatalRtlBuddyError("elaboration failed")
        if "KILL" in sv:
            return FpvFailResults(name="x", mode="bmc", depth=16)
        return FpvPassResults(name="x", mode="bmc", depth=16)


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


def test_mut_config_loads(tmp_path):
    mut_path = _write_project(tmp_path)
    cfg = MutSuiteConfig(path=str(mut_path)).get_config()
    assert cfg.get_name() == "leaf"
    assert cfg.top == "leaf"
    assert cfg.get_operators() == ["arith_flip", "cond_const", "assign_drop"]
    assert cfg.verification == "leaf_safety"
    assert cfg.budget.schedule == "round_robin"
    assert cfg.get_design_file().endswith("leaf.sv")
    assert os.path.isfile(cfg.get_design_file())


def test_mut_config_rejects_unknown_operator(tmp_path):
    mut_path = _write_project(tmp_path)
    text = mut_path.read_text().replace("arith_flip", "frobnicate")
    mut_path.write_text(text)
    with pytest.raises(FatalRtlBuddyError, match="operator 'frobnicate'"):
        MutSuiteConfig(path=str(mut_path))


def test_mut_config_rejects_bad_schedule(tmp_path):
    mut_path = _write_project(tmp_path)
    text = mut_path.read_text().replace("round_robin", "fifo")
    mut_path.write_text(text)
    with pytest.raises(FatalRtlBuddyError, match="schedule 'fifo'"):
        MutSuiteConfig(path=str(mut_path))


# ---------------------------------------------------------------------------
# Runner tests
# ---------------------------------------------------------------------------


def _runner(tmp_path, mut_path):
    from rtl_buddy.runner.mut_runner import MutRunner

    cfg = MutSuiteConfig(path=str(mut_path)).get_config()
    return MutRunner(
        name="test",
        root_cfg=None,
        mut_cfg=cfg,
        work_dir=str(tmp_path / "work"),
    )


def test_missing_xeno_raises_with_install_hint(tmp_path):
    # No stub_xeno fixture here, and xeno is not installed in CI.
    assert "rtl_buddy_xeno" not in sys.modules
    mut_path = _write_project(tmp_path)
    runner = _runner(tmp_path, mut_path)
    with pytest.raises(FatalRtlBuddyError, match="rtl-buddy-xeno"):
        runner.list_candidates()


def test_operator_mapping_to_kinds(tmp_path, stub_xeno):
    mut_path = _write_project(tmp_path)
    runner = _runner(tmp_path, mut_path)
    kinds = runner._kinds(stub_xeno)
    assert [k.value for k in kinds] == ["arith_flip", "cond_const", "assign_drop"]


def test_list_candidates(tmp_path, stub_xeno):
    mut_path = _write_project(tmp_path)
    runner = _runner(tmp_path, mut_path)
    sites = runner.list_candidates()
    assert len(sites) == 2
    assert sites[0]["operator"] == "arith_flip"
    assert sites[0]["line"] == 2


def test_run_scores_mutants(tmp_path, stub_xeno):
    mut_path = _write_project(tmp_path)
    runner = _runner(tmp_path, mut_path)
    with patch("rtl_buddy.runner.mut_runner.FpvRunner", _FakeFpvRunner):
        results = runner.run()

    assert isinstance(results, MutResults)
    assert results.baseline_verdict == "PASS"
    assert results.killed() == 1
    assert results.survived() == 1
    assert results.errored() == 1
    # score = killed / (killed + survived) = 1/2, errored dropped.
    assert results.score() == pytest.approx(0.5)

    outcomes = {o.operator: o.outcome for o in results.outcomes}
    assert outcomes["arith_flip"] == KILLED
    assert outcomes["cond_const"] == SURVIVED
    assert outcomes["assign_drop"] == ERRORED


def test_run_flags_predicted_observable_miss(tmp_path, stub_xeno):
    mut_path = _write_project(tmp_path)
    runner = _runner(tmp_path, mut_path)
    with patch("rtl_buddy.runner.mut_runner.FpvRunner", _FakeFpvRunner):
        results = runner.run()
    misses = results.predicted_observable_misses()
    # The survived COND_CONST mutant predicted perturbs_signals={cnt}.
    assert len(misses) == 1
    assert misses[0].operator == "cond_const"
    assert misses[0].predicted_signals == ["cnt"]


def test_run_does_not_touch_original_source(tmp_path, stub_xeno):
    mut_path = _write_project(tmp_path)
    original = (tmp_path / "design" / "leaf" / "leaf.sv").read_text()
    runner = _runner(tmp_path, mut_path)
    with patch("rtl_buddy.runner.mut_runner.FpvRunner", _FakeFpvRunner):
        runner.run()
    assert (tmp_path / "design" / "leaf" / "leaf.sv").read_text() == original


def test_design_file_outside_model_dir_errors(tmp_path, stub_xeno):
    mut_path = _write_project(tmp_path)
    # Point design_file outside the model directory.
    stray = tmp_path / "stray.sv"
    stray.write_text(_DESIGN_SV)
    text = mut_path.read_text().replace(
        'design_file: "../../design/leaf/leaf.sv"',
        f'design_file: "{stray}"',
    )
    mut_path.write_text(text)
    runner = _runner(tmp_path, mut_path)
    with patch("rtl_buddy.runner.mut_runner.FpvRunner", _FakeFpvRunner):
        with pytest.raises(FatalRtlBuddyError, match="must live within the model"):
            runner.run()


# ---------------------------------------------------------------------------
# Report round-trip
# ---------------------------------------------------------------------------


def test_report_round_trip(tmp_path, stub_xeno):
    mut_path = _write_project(tmp_path)
    runner = _runner(tmp_path, mut_path)
    with patch("rtl_buddy.runner.mut_runner.FpvRunner", _FakeFpvRunner):
        results = runner.run()
    report = results.as_report()
    restored = MutResults.from_report(report)
    assert restored.killed() == results.killed()
    assert restored.survived() == results.survived()
    assert restored.errored() == results.errored()
    assert restored.score() == results.score()
