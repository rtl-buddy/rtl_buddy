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


def test_missing_xeno_raises_with_install_hint(tmp_path, monkeypatch):
    # Force the import to fail regardless of whether rtl-buddy-xeno is
    # installed: a None entry in sys.modules makes the import raise.
    monkeypatch.setitem(sys.modules, "rtl_buddy_xeno", None)
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
    assert results.baseline_verdict == "fpv=PASS"
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


# ---------------------------------------------------------------------------
# Sim oracle
# ---------------------------------------------------------------------------

from dataclasses import dataclass as _dataclass  # noqa: E402
from rtl_buddy.config.model import ModelConfig as _ModelConfig  # noqa: E402
from rtl_buddy.runner.test_results import (  # noqa: E402
    CompileFailResults as _CompileFailResults,
    TestPassResults as _TestPassResults,
    TestResults as _TestResults,
)

_MUT_YAML_SIM = dedent(
    """\
    rtl-buddy-filetype: mut_config
    model: "leaf"
    model_path: "../../design/leaf/models.yaml"
    design_file: "../../design/leaf/leaf.sv"
    operators: [arith_flip, cond_const]
    verify:
      test_config: "tests.yaml"
    budget:
      max_mutants: 10
      schedule: round_robin
    """
)


def _write_sim_mut(root, body):
    """Write a mut.yaml (given body) plus a placeholder tests.yaml."""
    fpv_dir = root / "fpv" / "leaf"
    (fpv_dir / "tests.yaml").write_text("rtl-buddy-filetype: test_config\n")
    mut_path = fpv_dir / "mut.yaml"
    mut_path.write_text(body)
    return mut_path


@_dataclass
class _FakeTestConfig:
    name: str
    model: object
    assertions: bool


def _fake_suite_factory(model_yaml):
    class _FakeSuiteConfig:
        def __init__(self, path):
            self.path = path

        def get_tests(self, name=None):
            model = (
                _ModelConfig(name="leaf", filelist=["-v leaf.sv"], path=model_yaml)
                if model_yaml
                else None
            )
            return [_FakeTestConfig(name="basic", model=model, assertions=False)]

    return _FakeSuiteConfig


# Marker-driven fake TestRunner, mirroring _FakeFpvRunner's convention:
#   ERR -> compile failure (errored), KILL -> test FAIL (killed),
#   ASSERT -> passes but an assertion fired (killed), else PASS (survived).
class _FakeTestRunner:
    def __init__(
        self, name, root_cfg, test_cfg, rtl_builder_mode, test_runner_mode, suite_dir
    ):
        self.test_cfg = test_cfg

    def run(self):
        model = self.test_cfg.model
        sv = next(Path(os.path.dirname(model.path)).glob("*.sv")).read_text()
        if "ERR" in sv:
            return _CompileFailResults(name="t")
        if "KILL" in sv:
            return _TestResults(name="t", results={"result": "FAIL", "desc": "x"})
        if "ASSERT" in sv:
            r = _TestPassResults(name="t")
            r.results["assertions"] = {"enabled": True, "fired": 1}
            return r
        return _TestPassResults(name="t")


def _install_sim_fakes(monkeypatch, model_yaml=None):
    monkeypatch.setattr(
        "rtl_buddy.config.suite.SuiteConfig", _fake_suite_factory(model_yaml)
    )
    monkeypatch.setattr("rtl_buddy.runner.test_runner.TestRunner", _FakeTestRunner)


def _install_stub_xeno(monkeypatch, mutants):
    class _SingleMutator:
        @classmethod
        def from_sv(cls, path):
            return cls()

        def generate(self, kinds, count, seed=0, schedule=None):
            yield from mutants[:count]

        def candidates(self, kinds):
            return iter(())

    mod = types.ModuleType("rtl_buddy_xeno")
    mod.Mutator = _SingleMutator
    mod.MutationKind = _MutationKind
    mod.Schedule = _Schedule
    mod.Mutant = _Mutant
    mod.Prediction = _Prediction
    monkeypatch.setitem(sys.modules, "rtl_buddy_xeno", mod)


# ---- config ----


def test_mut_config_sim_only(tmp_path):
    _write_project(tmp_path)
    mut_path = _write_sim_mut(tmp_path, _MUT_YAML_SIM)
    cfg = MutSuiteConfig(path=str(mut_path)).get_config()
    assert cfg.has_sim_oracle()
    assert not cfg.has_fpv_oracle()
    assert cfg.test_config.endswith("tests.yaml")
    assert cfg.assertions is True


def test_mut_config_both_oracles(tmp_path):
    _write_project(tmp_path)
    body = _MUT_YAML_SIM.replace(
        'verify:\n  test_config: "tests.yaml"\n',
        'verify:\n  fpv_config: "fpv.yaml"\n  verification: "leaf_safety"\n'
        '  test_config: "tests.yaml"\n',
    )
    mut_path = _write_sim_mut(tmp_path, body)
    cfg = MutSuiteConfig(path=str(mut_path)).get_config()
    assert cfg.has_fpv_oracle() and cfg.has_sim_oracle()


def test_mut_config_no_oracle_errors(tmp_path):
    _write_project(tmp_path)
    # Valid verify mapping but no oracle fields -> validation must fire.
    body = _MUT_YAML_SIM.replace(
        '  test_config: "tests.yaml"\n', "  assertions: true\n"
    )
    mut_path = _write_sim_mut(tmp_path, body)
    with pytest.raises(FatalRtlBuddyError, match="at least one kill oracle"):
        MutSuiteConfig(path=str(mut_path))


# ---- sim scoring ----


def _sim_runner(tmp_path):
    from rtl_buddy.runner.mut_runner import MutRunner

    mut_path = _write_sim_mut(tmp_path, _MUT_YAML_SIM)
    cfg = MutSuiteConfig(path=str(mut_path)).get_config()
    return MutRunner(
        name="test", root_cfg=None, mut_cfg=cfg, work_dir=str(tmp_path / "work")
    )


@pytest.mark.parametrize(
    "marker,expected",
    [("KILL", KILLED), ("ASSERT", KILLED), ("survive", SURVIVED), ("ERR", ERRORED)],
)
def test_eval_sim_classification(tmp_path, monkeypatch, marker, expected):
    _write_project(tmp_path)
    _install_sim_fakes(monkeypatch, model_yaml=None)
    runner = _sim_runner(tmp_path)
    mutant_model, _root = runner._materialise_mutant(
        "m0", f"// {marker}\n" + _DESIGN_SV
    )
    outcome, verdict = runner._eval_sim(mutant_model, "m0")
    assert outcome == expected
    assert verdict.startswith("sim=")


def test_run_sim_oracle_scores(tmp_path, monkeypatch):
    _write_project(tmp_path)
    model_yaml = str(tmp_path / "design" / "leaf" / "models.yaml")
    _install_sim_fakes(monkeypatch, model_yaml=model_yaml)
    _install_stub_xeno(
        monkeypatch,
        [
            _Mutant(
                "// KILL\n" + _DESIGN_SV,
                "k",
                1,
                _Prediction(),
                _MutationKind.ARITH_FLIP,
            ),
            _Mutant(
                "// survive\n" + _DESIGN_SV,
                "s",
                2,
                _Prediction(),
                _MutationKind.COND_CONST,
            ),
            _Mutant(
                "// ERR\n" + _DESIGN_SV, "e", 3, _Prediction(), _MutationKind.ARITH_FLIP
            ),
        ],
    )
    runner = _sim_runner(tmp_path)
    results = runner.run()
    assert results.baseline_verdict == "sim=PASS"
    assert results.killed() == 1
    assert results.survived() == 1
    assert results.errored() == 1


def test_both_oracles_union_kill(tmp_path, monkeypatch):
    # A mutant that the FPV proof misses but the sim assertion catches:
    # "// ASSERT" has no KILL marker (fpv -> PASS/survived) but fires an
    # assertion in sim -> the union verdict must be killed.
    _write_project(tmp_path)
    model_yaml = str(tmp_path / "design" / "leaf" / "models.yaml")
    _install_sim_fakes(monkeypatch, model_yaml=model_yaml)
    _install_stub_xeno(
        monkeypatch,
        [
            _Mutant(
                "// ASSERT\n" + _DESIGN_SV,
                "a",
                1,
                _Prediction(),
                _MutationKind.ARITH_FLIP,
            )
        ],
    )
    body = _MUT_YAML_SIM.replace(
        'verify:\n  test_config: "tests.yaml"\n',
        'verify:\n  fpv_config: "fpv.yaml"\n  verification: "leaf_safety"\n'
        '  test_config: "tests.yaml"\n',
    )
    from rtl_buddy.runner.mut_runner import MutRunner

    mut_path = _write_sim_mut(tmp_path, body)
    cfg = MutSuiteConfig(path=str(mut_path)).get_config()
    runner = MutRunner(
        name="test", root_cfg=None, mut_cfg=cfg, work_dir=str(tmp_path / "work")
    )
    with patch("rtl_buddy.runner.mut_runner.FpvRunner", _FakeFpvRunner):
        results = runner.run()
    assert results.killed() == 1
    assert results.survived() == 0
    o = results.outcomes[0]
    assert o.outcome == KILLED
    assert "fpv=PASS" in o.verdict and "sim=FAIL" in o.verdict


# ---------------------------------------------------------------------------
# Scope graph-ingestion: a 2-module hierarchy (hier_top -> two leaf insts)
# ---------------------------------------------------------------------------

_HIER_TOP_SV = dedent(
    """\
    module hier_top (input logic clk, input logic en,
                     output logic [2:0] a, output logic [2:0] b);
      leaf u_alu_a (.clk(clk), .en(en), .cnt(a));
      leaf u_alu_b (.clk(clk), .en(en), .cnt(b));
    endmodule
    """
)


def _write_hier_project(root: Path, scope_block: str) -> Path:
    """A two-file hierarchy: hier_top.sv instantiates two leaf instances
    from leaf.sv. Returns the mut.yaml path. ``scope_block`` is spliced
    into mut.yaml verbatim (already indented as a top-level YAML key)."""
    design_dir = root / "design" / "hier"
    design_dir.mkdir(parents=True)
    (design_dir / "leaf.sv").write_text(_DESIGN_SV)
    (design_dir / "hier_top.sv").write_text(_HIER_TOP_SV)
    (design_dir / "models.yaml").write_text(
        dedent(
            """\
            rtl-buddy-filetype: model_config
            models:
              - name: "hier_top"
                filelist: ["-v hier_top.sv", "-v leaf.sv"]
            """
        )
    )

    fpv_dir = root / "fpv" / "hier"
    fpv_dir.mkdir(parents=True)
    (fpv_dir / "fpv.yaml").write_text(
        dedent(
            """\
            rtl-buddy-filetype: fpv_config
            verifications:
              - name: "hier_safety"
                desc: "safety"
                tool: "sby"
                model: "hier_top"
                model_path: "../../design/hier/models.yaml"
                top: "hier_top"
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
            model: "hier_top"
            model_path: "../../design/hier/models.yaml"
            design_file: "../../design/hier/leaf.sv"
            operators: [arith_flip, cond_const]
            verify:
              fpv_config: "fpv.yaml"
              verification: "hier_safety"
            budget:
              max_mutants: 10
              schedule: sequential
            """
        )
        + scope_block
    )
    return mut_path


def _hier_runner(tmp_path, mut_path):
    from rtl_buddy.runner.mut_runner import MutRunner

    cfg = MutSuiteConfig(path=str(mut_path)).get_config()
    return MutRunner(
        name="test", root_cfg=None, mut_cfg=cfg, work_dir=str(tmp_path / "work")
    )


@pytest.fixture
def stub_hier(monkeypatch):
    """Patch MutRunner._scope_graph_json with a canned 2-module graph so
    no real rtl-buddy-view subprocess is needed. Source files are derived
    from the configured design_file's directory (design/hier/)."""

    def _graph(self):
        d = os.path.dirname(os.path.abspath(self.mut_cfg.get_design_file()))
        return {
            "schema_version": "1.1",
            "top": "hier_top",
            "edges": [],
            "overlays_present": [],
            "nodes": [
                {
                    "id": "hier_top",
                    "module": "hier_top",
                    "source": {"file": os.path.join(d, "hier_top.sv")},
                },
                {
                    "id": "hier_top.u_alu_a",
                    "module": "leaf",
                    "source": {"file": os.path.join(d, "leaf.sv")},
                },
                {
                    "id": "hier_top.u_alu_b",
                    "module": "leaf",
                    "source": {"file": os.path.join(d, "leaf.sv")},
                },
            ],
        }

    monkeypatch.setattr(
        "rtl_buddy.runner.mut_runner.MutRunner._scope_graph_json", _graph
    )


# A scope-aware fake FpvRunner: scans every .sv in the spliced model tree
# for a KILL / ERR marker (the multi-file tree has more than one .sv, so we
# can't rely on the single-glob convention _FakeFpvRunner uses).
class _ScopeFakeFpvRunner:
    def __init__(self, name, root_cfg, fpv_cfg, suite_dir):
        self.fpv_cfg = fpv_cfg

    def run(self):
        model = self.fpv_cfg.get_model()
        design_dir = Path(os.path.dirname(model.path))
        text = "".join(p.read_text() for p in sorted(design_dir.glob("*.sv")))
        if "ERR" in text:
            raise FatalRtlBuddyError("elaboration failed")
        if "KILL" in text:
            return FpvFailResults(name="x", mode="bmc", depth=16)
        return FpvPassResults(name="x", mode="bmc", depth=16)


def _install_scope_xeno(monkeypatch):
    """A xeno stub whose Mutator is keyed by the file it was built from, so
    every scoped file yields a mutant carrying a marker derived from that
    file's basename. Lets a test prove each mutant was spliced into ITS
    origin file."""

    class _ScopeMutator:
        def __init__(self, basename, text):
            self.basename = basename
            self.text = text

        @classmethod
        def from_sv(cls, path):
            p = Path(path)
            return cls(p.name, p.read_text())

        def generate(self, kinds, count, seed=0, schedule=None):
            # One mutant per file. Its sv prepends a per-file marker so the
            # materialised file is identifiable.
            yield _Mutant(
                sv=f"// MUT {self.basename}\n" + self.text,
                diff_summary=f"mutate {self.basename}",
                seed=1,
                prediction=_Prediction(),
                kind=_MutationKind.ARITH_FLIP,
            )

        def candidates(self, kinds):
            yield _Site(_MutationKind.ARITH_FLIP, 2, 1, "+", _Prediction())

    mod = types.ModuleType("rtl_buddy_xeno")
    mod.Mutator = _ScopeMutator
    mod.MutationKind = _MutationKind
    mod.Schedule = _Schedule
    mod.Mutant = _Mutant
    mod.Site = _Site
    mod.Prediction = _Prediction
    monkeypatch.setitem(sys.modules, "rtl_buddy_xeno", mod)
    return mod


def test_scope_empty_skips_graph(tmp_path, stub_xeno, monkeypatch):
    # Default (no scope) project: the graph resolver must never be called.
    mut_path = _write_project(tmp_path)
    runner = _runner(tmp_path, mut_path)

    def _boom(self):
        raise AssertionError("_scope_graph_json must not run for empty scope")

    monkeypatch.setattr(
        "rtl_buddy.runner.mut_runner.MutRunner._scope_graph_json", _boom
    )
    # list works, run scores, neither touches the graph.
    assert len(runner.list_candidates()) == 2
    with patch("rtl_buddy.runner.mut_runner.FpvRunner", _FakeFpvRunner):
        results = runner.run()
    assert results.killed() == 1
    assert results.survived() == 1
    # No per-file breakdown for the single-file path.
    assert results.per_file is None
    assert "per_file" not in results.as_report()


def test_scope_include_selects_files(tmp_path, stub_xeno, stub_hier):
    mut_path = _write_hier_project(
        tmp_path,
        dedent(
            """\
            scope:
              include: ["*/leaf.sv"]
            """
        ),
    )
    runner = _hier_runner(tmp_path, mut_path)
    files = runner._scoped_source_files()
    assert len(files) == 1
    assert files[0].endswith("leaf.sv")
    # list_candidates tags each site with the model-relative origin file.
    sites = runner.list_candidates()
    assert sites and all(s["file"] == "leaf.sv" for s in sites)


def test_scope_exclude_drops_files(tmp_path, stub_xeno, stub_hier):
    mut_path = _write_hier_project(
        tmp_path,
        dedent(
            """\
            scope:
              exclude: ["*/leaf.sv"]
            """
        ),
    )
    runner = _hier_runner(tmp_path, mut_path)
    files = runner._scoped_source_files()
    # Excluding leaf.sv leaves only hier_top.sv.
    assert len(files) == 1
    assert files[0].endswith("hier_top.sv")


def test_scope_instance_path_glob(tmp_path, stub_xeno, stub_hier):
    # Match by dotted instance path, not by file glob.
    mut_path = _write_hier_project(
        tmp_path,
        dedent(
            """\
            scope:
              include: ["hier_top.u_alu_a"]
            """
        ),
    )
    runner = _hier_runner(tmp_path, mut_path)
    files = runner._scoped_source_files()
    # The instance maps to leaf.sv; only that file is in scope.
    assert len(files) == 1
    assert files[0].endswith("leaf.sv")


def test_scope_empty_selection_errors(tmp_path, stub_xeno, stub_hier):
    mut_path = _write_hier_project(
        tmp_path,
        dedent(
            """\
            scope:
              include: ["*/nonexistent.sv"]
            """
        ),
    )
    runner = _hier_runner(tmp_path, mut_path)
    with pytest.raises(FatalRtlBuddyError, match="selected no source files"):
        runner._scoped_source_files()


def test_scope_missing_view_binary(tmp_path, stub_xeno, monkeypatch):
    # Do NOT stub _scope_graph_json; instead make RtlBuddyView.run raise as
    # the real wrapper does when the binary is absent, and assert run()
    # propagates it.
    mut_path = _write_hier_project(
        tmp_path,
        dedent(
            """\
            scope:
              include: ["*/leaf.sv"]
            """
        ),
    )

    def _no_binary(self):
        raise FatalRtlBuddyError(
            "hier: 'rtl-buddy-view' not found on PATH; install rtl-buddy-view"
        )

    monkeypatch.setattr(
        "rtl_buddy.tools.hier_rtl_buddy_view.RtlBuddyView.run", _no_binary
    )
    runner = _hier_runner(tmp_path, mut_path)
    with pytest.raises(FatalRtlBuddyError, match="rtl-buddy-view"):
        with patch("rtl_buddy.runner.mut_runner.FpvRunner", _ScopeFakeFpvRunner):
            runner.run()


def test_scope_multifile_run(tmp_path, monkeypatch, stub_hier):
    # Scope selects BOTH files; the stub yields one mutant per file, each
    # spliced into ITS origin file. Both are scored, with a per-file
    # breakdown in the report.
    mut_path = _write_hier_project(
        tmp_path,
        dedent(
            """\
            scope:
              include: ["*/hier_top.sv", "*/leaf.sv"]
            """
        ),
    )
    _install_scope_xeno(monkeypatch)
    runner = _hier_runner(tmp_path, mut_path)
    with patch("rtl_buddy.runner.mut_runner.FpvRunner", _ScopeFakeFpvRunner):
        results = runner.run()

    # One mutant per scoped file = two outcomes, both survived (no KILL/ERR
    # marker -> the fake oracle PASSes == baseline).
    assert len(results.outcomes) == 2
    origin_files = sorted(o.file for o in results.outcomes)
    assert origin_files == ["hier_top.sv", "leaf.sv"]

    # Each mutant must have been spliced into its OWN origin file: the
    # materialised model_src must carry the per-file marker in the matching
    # file and leave the other file's marker absent.
    for o in results.outcomes:
        # The model dir is design/hier (it holds models.yaml), so the copied
        # tree's root IS that dir and the model-relative file sits directly
        # under model_src.
        model_src = tmp_path / "work" / o.mutant_id / "model_src"
        spliced = (model_src / o.file).read_text()
        assert f"// MUT {o.file}" in spliced
        other = "leaf.sv" if o.file == "hier_top.sv" else "hier_top.sv"
        assert "// MUT" not in (model_src / other).read_text()

    # Per-file breakdown present and summing to the scored totals.
    report = results.as_report()
    assert set(report["per_file"]) == {"hier_top.sv", "leaf.sv"}
    total = sum(
        v[SURVIVED] + v[KILLED] + v[ERRORED] for v in report["per_file"].values()
    )
    assert total == 2


def test_scope_glob_is_case_sensitive(tmp_path, stub_xeno, stub_hier):
    # The scope globs use fnmatchcase, so matching is case-sensitive on
    # every platform (fnmatch would case-fold on macOS). A wrong-case
    # pattern must select NOTHING -> empty selection is a fatal error.
    wrong_case = _write_hier_project(
        tmp_path / "wrong",
        dedent(
            """\
            scope:
              include: ["*/LEAF.SV"]
            """
        ),
    )
    runner = _hier_runner(tmp_path / "wrong", wrong_case)
    with pytest.raises(FatalRtlBuddyError, match="selected no source files"):
        runner._scoped_source_files()

    # A wrong-case instance-path pattern is likewise inert.
    wrong_inst = _write_hier_project(
        tmp_path / "wrong_inst",
        dedent(
            """\
            scope:
              include: ["HIER_TOP.*"]
            """
        ),
    )
    runner = _hier_runner(tmp_path / "wrong_inst", wrong_inst)
    with pytest.raises(FatalRtlBuddyError, match="selected no source files"):
        runner._scoped_source_files()

    # The correct-case pattern selects the matching file.
    right_case = _write_hier_project(
        tmp_path / "right",
        dedent(
            """\
            scope:
              include: ["*/leaf.sv"]
            """
        ),
    )
    runner = _hier_runner(tmp_path / "right", right_case)
    files = runner._scoped_source_files()
    assert len(files) == 1
    assert files[0].endswith("leaf.sv")
