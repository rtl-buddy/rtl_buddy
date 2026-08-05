"""Tests for #378 — the post-merge binding stage of ``rb graph build``.

The stage answers "which tests drive this port?" and "which golden model
does this test check against?", which is the one question neither the
design tier (it has never heard of Python) nor the config tier (it has
never heard of ``dut.a``) can answer alone.

Three layers are covered here:

* the scanner (:func:`~rtl_buddy.graph.binding.scan_python_source`) —
  pure, no project needed;
* :func:`~rtl_buddy.graph.binding.bind_python` over a hand-built merged
  graph, which is where the confidence rules and the Graphify hand-off
  live;
* ``build_graph`` end to end on a real cocotb-shaped project, with
  ``rtl-buddy-view`` stubbed, mirroring the template's
  ``verif/demo_tiny_alu_cocotb/`` suite.

The project is written into ``tmp_path`` rather than kept under
``tests/fixtures/``: the cocotb modules have to be named ``test_*.py``
to be realistic, and pytest would collect those.
"""

from __future__ import annotations

import json
import shutil
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.graph import (
    PY_NODE_PREFIX,
    PYTHON_MODULE_TYPE,
    bind_python,
    build_graph,
    scan_python_source,
)
from rtl_buddy.graph.binding import resolve_module_file
from rtl_buddy.rtl_buddy import RtlBuddy

_FIXTURES = Path(__file__).parent / "fixtures"

ALU_PORTS = ("clk", "rst", "op", "a", "b", "y", "zf")


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def test_scan_finds_dut_accesses_with_lines_and_ignores_look_alikes(tmp_path: Path):
    src = tmp_path / "t.py"
    src.write_text(
        "import cocotb\n"
        "\n"
        "@cocotb.test()\n"
        "async def t(dut):\n"
        "    dut.rst.value = 1\n"
        "    x = int(dut.y.value)\n"
        "    note = 'dut.not_a_signal'\n"
        "    other.dut.nope = 1\n"
        "    dut._log.info('hi')\n"
        "    dut.value = 2\n"
        "    return x\n"
    )
    scan = scan_python_source(src)
    assert scan.parsed is True
    # `dut.rst` on line 5, `dut.y` on line 6. The string, the attribute
    # reached through another object, the private handle attribute and
    # the handle API attribute are all not signals.
    assert scan.accesses == {"rst": 5, "y": 6}
    assert scan.imports == ["cocotb"]


def test_scan_takes_the_handle_name_from_the_cocotb_test_signature(tmp_path: Path):
    src = tmp_path / "t.py"
    src.write_text(
        "import cocotb\n"
        "\n"
        "@cocotb.test()\n"
        "async def t(alu):\n"
        "    alu.op.value = 1\n"
        "\n"
        "def helper(dut):\n"
        "    dut.a.value = 2\n"
    )
    scan = scan_python_source(src)
    assert set(scan.accesses) == {"op", "a"}


def test_scan_falls_back_to_regex_when_the_file_does_not_parse(tmp_path: Path):
    src = tmp_path / "broken.py"
    src.write_text("from helper import thing\ndef f(:\n    dut.a.value = 1\n")
    scan = scan_python_source(src)
    assert scan.parsed is False
    assert scan.accesses == {"a": 3}
    assert scan.imports == ["helper"]


def test_scan_keeps_absolute_imports_and_drops_relative_ones(tmp_path: Path):
    src = tmp_path / "t.py"
    src.write_text(
        "import os.path\nfrom alu_model import Model\nfrom . import sibling\n"
    )
    scan = scan_python_source(src)
    assert scan.imports == ["os.path", "alu_model"]


def test_scan_of_an_unreadable_file_is_empty_not_an_error(tmp_path: Path):
    scan = scan_python_source(tmp_path / "gone.py")
    assert scan.accesses == {} and scan.imports == []


def test_resolve_module_file_handles_modules_and_packages(tmp_path: Path):
    (tmp_path / "flat.py").write_text("")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "deep.py").write_text("")
    assert resolve_module_file("flat", [tmp_path]) == tmp_path / "flat.py"
    assert resolve_module_file("pkg", [tmp_path]) == tmp_path / "pkg" / "__init__.py"
    assert resolve_module_file("pkg.deep", [tmp_path]) == tmp_path / "pkg" / "deep.py"
    assert resolve_module_file("nope", [tmp_path]) is None


# ---------------------------------------------------------------------------
# bind_python() over a hand-built merged graph
# ---------------------------------------------------------------------------


def _merged(nodes: list[dict], links: list[dict]) -> dict:
    return {
        "directed": True,
        "multigraph": True,
        "graph": {},
        "nodes": nodes,
        "links": links,
    }


def _config_nodes(
    *, suite: str = "verif/alu", modules: list[str] | None = None, toplevel: str = "alu"
) -> tuple[list[dict], list[dict]]:
    """The config-tier half the stage reads: one cocotb test on one testbench."""
    test_id = f"test:{suite}#t_cocotb"
    tb_id = f"tb:{suite}#tb_cocotb"
    nodes = [
        {
            "id": test_id,
            "type": "test",
            "label": "t_cocotb",
            "tier": "config",
            "cocotb_modules": modules if modules is not None else ["test_alu"],
        },
        {
            "id": tb_id,
            "type": "testbench",
            "label": "tb_cocotb",
            "tier": "config",
            "toplevel": toplevel,
        },
    ]
    links = [
        {
            "source": test_id,
            "target": tb_id,
            "type": "runs_on",
            "confidence": "EXTRACTED",
        }
    ]
    return nodes, links


def _design_nodes(top: str = "alu", ports: tuple[str, ...] = ALU_PORTS) -> list[dict]:
    nodes = [{"id": f"module:{top}", "type": "module", "label": top, "tier": "design"}]
    nodes += [
        {"id": f"port:{top}.{p}", "type": "port", "label": p, "tier": "design"}
        for p in ports
    ]
    return nodes


def _links_of(stage, link_type: str) -> list[dict]:
    return [x for x in stage.graph["links"] if x["type"] == link_type]


def _write_cocotb_module(root: Path, suite: str, name: str, body: str) -> Path:
    path = root / suite / f"{name}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_test_reaches_the_dut_module_in_two_hops(tmp_path: Path):
    _write_cocotb_module(
        tmp_path,
        "verif/alu",
        "test_alu",
        "import cocotb\n\n@cocotb.test()\nasync def t(dut):\n    dut.a.value = 1\n",
    )
    nodes, links = _config_nodes()
    stage = bind_python(_merged(nodes + _design_nodes(), links), tmp_path)

    assert stage.status == "built"
    py_id = PY_NODE_PREFIX + "verif/alu/test_alu.py"
    binds = {(x["source"], x["target"]) for x in _links_of(stage, "binds_to")}
    assert ("test:verif/alu#t_cocotb", py_id) in binds
    assert (py_id, "module:alu") in binds
    assert all(x["confidence"] == "EXTRACTED" for x in _links_of(stage, "binds_to"))

    node = next(n for n in stage.graph["nodes"] if n["id"] == py_id)
    assert node["type"] == PYTHON_MODULE_TYPE
    assert node["tier"] == "binding"
    assert node["file"] == "verif/alu/test_alu.py"
    assert node["cocotb_module"] is True


def test_dut_access_matching_a_port_is_extracted(tmp_path: Path):
    _write_cocotb_module(
        tmp_path,
        "verif/alu",
        "test_alu",
        "import cocotb\n\n@cocotb.test()\nasync def t(dut):\n"
        "    dut.a.value = 1\n    dut.op.value = 2\n",
    )
    nodes, links = _config_nodes()
    stage = bind_python(_merged(nodes + _design_nodes(), links), tmp_path)

    drives = {x["target"]: x for x in _links_of(stage, "drives")}
    assert set(drives) == {"port:alu.a", "port:alu.op"}
    assert drives["port:alu.a"]["confidence"] == "EXTRACTED"
    assert drives["port:alu.a"]["signal"] == "a"
    assert drives["port:alu.a"]["file"] == "verif/alu/test_alu.py"
    assert drives["port:alu.a"]["line"] == 5
    assert "via" not in drives["port:alu.a"]
    assert stage.extracted == 2 and stage.inferred == 0


def test_dut_access_that_is_no_port_is_inferred_and_flagged(tmp_path: Path):
    _write_cocotb_module(
        tmp_path,
        "verif/alu",
        "test_alu",
        "import cocotb\n\n@cocotb.test()\nasync def t(dut):\n"
        "    dut.a.value = 1\n    dut.bus.valid.value = 1\n",
    )
    nodes, links = _config_nodes()
    stage = bind_python(_merged(nodes + _design_nodes(), links), tmp_path)

    drives = {x["target"]: x for x in _links_of(stage, "drives")}
    assert drives["port:alu.a"]["confidence"] == "EXTRACTED"
    # A bus wrapper is not a port of the toplevel; the edge survives so
    # the access is visible, tagged so a consumer can filter it out.
    assert drives["port:alu.bus"]["confidence"] == "INFERRED"
    assert drives["port:alu.bus"]["resolved"] is False
    assert stage.extracted == 1 and stage.inferred == 1
    assert any(u.get("access") == "dut.bus" for u in stage.unresolved)


def test_case_only_mismatch_points_at_the_real_port_as_inferred(tmp_path: Path):
    _write_cocotb_module(
        tmp_path,
        "verif/alu",
        "test_alu",
        "import cocotb\n\n@cocotb.test()\nasync def t(dut):\n    dut.CLK.value = 1\n",
    )
    nodes, links = _config_nodes()
    stage = bind_python(_merged(nodes + _design_nodes(), links), tmp_path)

    drive = _links_of(stage, "drives")[0]
    assert drive["target"] == "port:alu.clk"
    assert drive["confidence"] == "INFERRED"
    assert drive["signal"] == "CLK"


def test_without_a_design_tier_every_drive_is_inferred(tmp_path: Path):
    _write_cocotb_module(
        tmp_path,
        "verif/alu",
        "test_alu",
        "import cocotb\n\n@cocotb.test()\nasync def t(dut):\n    dut.a.value = 1\n",
    )
    nodes, links = _config_nodes()
    # No `port:` nodes at all — `rb graph build --no-design`, or a model
    # the viewer could not export. There is nothing to check the name
    # against, so the edge cannot claim to be EXTRACTED.
    stage = bind_python(_merged(nodes, links), tmp_path)

    drive = _links_of(stage, "drives")[0]
    assert drive["target"] == "port:alu.a"
    assert drive["confidence"] == "INFERRED"
    assert "resolved" not in drive  # unknown, not known-wrong


def test_accesses_reached_through_a_helper_carry_via(tmp_path: Path):
    _write_cocotb_module(
        tmp_path,
        "verif/alu",
        "_common",
        "async def drive(dut, value):\n    dut.a.value = value\n",
    )
    _write_cocotb_module(
        tmp_path,
        "verif/alu",
        "test_alu",
        "import cocotb\nfrom _common import drive\n\n"
        "@cocotb.test()\nasync def t(dut):\n    await drive(dut, 1)\n"
        "    dut.op.value = 0\n",
    )
    nodes, links = _config_nodes()
    stage = bind_python(_merged(nodes + _design_nodes(), links), tmp_path)

    entry = PY_NODE_PREFIX + "verif/alu/test_alu.py"
    helper = PY_NODE_PREFIX + "verif/alu/_common.py"
    drives = {(x["source"], x["target"]): x for x in _links_of(stage, "drives")}

    # First-hand: the helper really does say `dut.a`.
    assert (helper, "port:alu.a") in drives
    assert "via" not in drives[(helper, "port:alu.a")]
    # Inherited by the cocotb module through the import, and labelled so.
    assert drives[(entry, "port:alu.a")]["via"] == "verif/alu/_common.py"
    # Its own access is not marked via anything.
    assert "via" not in drives[(entry, "port:alu.op")]
    # And the helper is reachable, which is what `imports` is for.
    assert (entry, helper) in {
        (x["source"], x["target"]) for x in _links_of(stage, "imports")
    }


def test_golden_model_import_becomes_checks_against(tmp_path: Path):
    _write_cocotb_module(
        tmp_path,
        "verif/alu",
        "_common",
        "from alu_model import Model\n\ndef check(dut):\n    return Model\n",
    )
    _write_cocotb_module(
        tmp_path,
        "verif/alu",
        "test_alu",
        "import cocotb\nfrom _common import check\n\n"
        "@cocotb.test()\nasync def t(dut):\n    check(dut)\n",
    )
    nodes, links = _config_nodes()
    nodes = nodes + [
        {
            "id": "golden:spec/alu/alu_model.py",
            "type": "golden_model",
            "label": "alu_model",
            "tier": "config",
            "file": "spec/alu/alu_model.py",
        }
    ]
    stage = bind_python(_merged(nodes + _design_nodes(), links), tmp_path)

    checks = _links_of(stage, "checks_against")
    assert len(checks) == 1
    assert checks[0]["source"] == "test:verif/alu#t_cocotb"
    assert checks[0]["target"] == "golden:spec/alu/alu_model.py"
    # Reached through the helper, not imported by the test itself.
    assert checks[0]["via"] == "verif/alu/_common.py"
    # A golden model is not turned into a python_module node — the config
    # tier already owns it.
    assert PY_NODE_PREFIX + "spec/alu/alu_model.py" not in {
        n["id"] for n in stage.graph["nodes"]
    }


def test_a_direct_golden_import_wins_over_the_transitive_one(tmp_path: Path):
    _write_cocotb_module(tmp_path, "verif/alu", "_common", "from alu_model import M\n")
    _write_cocotb_module(
        tmp_path,
        "verif/alu",
        "test_alu",
        "import cocotb\nimport _common\nfrom alu_model import M\n",
    )
    nodes, links = _config_nodes()
    nodes = nodes + [
        {
            "id": "golden:spec/alu/alu_model.py",
            "type": "golden_model",
            "label": "alu_model",
            "tier": "config",
            "file": "spec/alu/alu_model.py",
        }
    ]
    stage = bind_python(_merged(nodes + _design_nodes(), links), tmp_path)

    checks = _links_of(stage, "checks_against")
    assert len(checks) == 1
    assert "via" not in checks[0]


def test_an_existing_python_node_id_is_reused_instead_of_synthesized(tmp_path: Path):
    _write_cocotb_module(
        tmp_path,
        "verif/alu",
        "test_alu",
        "import cocotb\n\n@cocotb.test()\nasync def t(dut):\n    dut.a.value = 1\n",
    )
    nodes, links = _config_nodes()
    # What Graphify contributes: its own id for the same file. The stage
    # matches on `file`, so it must bind to that node rather than invent
    # a second one for the same module.
    nodes = nodes + [
        {
            "id": "pymod:verif/alu/test_alu.py",
            "type": "python_module",
            "label": "test_alu",
            "tier": "binding",
            "file": "verif/alu/test_alu.py",
        }
    ]
    stage = bind_python(_merged(nodes + _design_nodes(), links), tmp_path)

    ids = {n["id"] for n in stage.graph["nodes"]}
    assert "pymod:verif/alu/test_alu.py" in ids
    assert PY_NODE_PREFIX + "verif/alu/test_alu.py" not in ids
    assert stage.reused_ids == 1
    assert any(
        x["target"] == "pymod:verif/alu/test_alu.py"
        for x in _links_of(stage, "binds_to")
    )


def test_a_missing_cocotb_module_still_binds_and_is_reported(tmp_path: Path):
    nodes, links = _config_nodes(modules=["test_typo"])
    stage = bind_python(_merged(nodes + _design_nodes(), links), tmp_path)

    node = next(n for n in stage.graph["nodes"] if n["id"].startswith(PY_NODE_PREFIX))
    assert node["id"] == PY_NODE_PREFIX + "verif/alu/test_typo.py"
    assert node["exists"] is False
    assert stage.unresolved == [
        {
            "test": "test:verif/alu#t_cocotb",
            "cocotb_module": "test_typo",
            "expected": "verif/alu/test_typo.py",
        }
    ]
    # The test is still tied to the DUT: a typo'd module name should be
    # visible in the graph, not erase the binding.
    assert stage.tests == 1
    assert len(_links_of(stage, "binds_to")) == 2


def test_a_graph_with_no_cocotb_tests_is_skipped(tmp_path: Path):
    nodes = [
        {
            "id": "test:verif/alu#t_hdl",
            "type": "test",
            "label": "t_hdl",
            "tier": "config",
        }
    ]
    stage = bind_python(_merged(nodes, []), tmp_path)
    assert stage.status == "skipped"
    assert stage.detail == "no cocotb tests in the graph"
    assert stage.graph["nodes"] == [] and stage.graph["links"] == []


def test_binding_output_is_deterministic(tmp_path: Path):
    _write_cocotb_module(
        tmp_path,
        "verif/alu",
        "_common",
        "def drive(dut):\n    dut.b.value = 1\n    dut.a.value = 2\n",
    )
    _write_cocotb_module(
        tmp_path,
        "verif/alu",
        "test_alu",
        "import cocotb\nimport _common\n\n@cocotb.test()\nasync def t(dut):\n"
        "    _common.drive(dut)\n",
    )
    nodes, links = _config_nodes()
    merged = _merged(nodes + _design_nodes(), links)
    first = bind_python(merged, tmp_path).graph
    second = bind_python(merged, tmp_path).graph
    assert json.dumps(first) == json.dumps(second)


def test_an_import_cycle_terminates(tmp_path: Path):
    _write_cocotb_module(tmp_path, "verif/alu", "a_mod", "import b_mod\n")
    _write_cocotb_module(tmp_path, "verif/alu", "b_mod", "import a_mod\n")
    _write_cocotb_module(
        tmp_path, "verif/alu", "test_alu", "import a_mod\n\ndef f(dut):\n    dut.a\n"
    )
    nodes, links = _config_nodes()
    stage = bind_python(_merged(nodes + _design_nodes(), links), tmp_path)
    assert stage.status == "built"
    assert {x["source"] for x in _links_of(stage, "imports")}


# ---------------------------------------------------------------------------
# build_graph() end to end
# ---------------------------------------------------------------------------


_ALU_SV = """\
module alu (
  input  logic       clk,
  input  logic       rst,
  input  logic [2:0] op,
  input  logic [7:0] a,
  input  logic [7:0] b,
  output logic [7:0] y,
  output logic       zf
);
endmodule
"""

_TESTS_YAML = """\
rtl-buddy-filetype: test_config

testbenches:
  - name: "tb_cocotb"
    filelist: []
    toplevel: alu
    cocotb:
      module: test_alu

tests:
  - name: "cocotb_basic"
    desc: "cocotb cosim against the golden model"
    reglvl: 0
    model: "alu"
    model_path: "../../design/alu/models.yaml"
    testbench: "tb_cocotb"
"""

_HELPER_PY = """\
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "spec" / "alu"))
from alu_model import AluModel  # noqa: E402


async def drive(dut, op, a, b):
    dut.op.value = op
    dut.a.value = a
    dut.b.value = b
    assert int(dut.y.value) == AluModel.compute(op, a, b)
"""

_TEST_PY = """\
import cocotb

from _alu_common import drive


@cocotb.test()
async def cocotb_basic(dut):
    dut.rst.value = 1
    dut.clk.value = 0
    await drive(dut, 0, 1, 2)
"""


@pytest.fixture
def cocotb_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A miniature of the template's ``verif/demo_tiny_alu_cocotb`` suite."""
    root = tmp_path / "project"
    (root / "design" / "alu").mkdir(parents=True)
    (root / "spec" / "alu").mkdir(parents=True)
    (root / "verif" / "alu_cocotb").mkdir(parents=True)

    shutil.copy(_FIXTURES / "minimal_project" / "root_config.yaml", root)
    (root / "regression.yaml").write_text(
        "rtl-buddy-filetype: reg_config\ntest-configs:\n  - verif/alu_cocotb/tests.yaml\n"
    )
    (root / "design" / "alu" / "alu.sv").write_text(_ALU_SV)
    (root / "design" / "alu" / "models.yaml").write_text(
        "rtl-buddy-filetype: model_config\n\nmodels:\n"
        '  - name: "alu"\n    desc: "ALU"\n    filelist: ["alu.sv"]\n'
        '    spec: "../../spec/alu/specs.yaml"\n'
    )
    (root / "spec" / "alu" / "specs.yaml").write_text(
        "rtl-buddy-filetype: spec_config\n\nblocks:\n"
        '  - name: "alu"\n    desc: "ALU block"\n'
    )
    (root / "spec" / "alu" / "alu_model.py").write_text(
        "class AluModel:\n"
        "    @staticmethod\n"
        "    def compute(op, a, b):\n"
        "        return a + b\n"
    )
    (root / "verif" / "alu_cocotb" / "tests.yaml").write_text(_TESTS_YAML)
    (root / "verif" / "alu_cocotb" / "_alu_common.py").write_text(_HELPER_PY)
    (root / "verif" / "alu_cocotb" / "test_alu.py").write_text(_TEST_PY)

    monkeypatch.chdir(root)
    return root


def _fake_view(tmp_path: Path) -> Path:
    """Stub ``rtl-buddy-view graph`` emitting ``alu``'s module and ports."""
    graph = {
        "directed": True,
        "multigraph": True,
        "graph": {
            "schema_version": 1,
            "generator": {
                "tool": "rtl-buddy-view",
                "version": "0.4.0",
                "tier": "design",
            },
            "design": {"top": "alu"},
        },
        "nodes": _design_nodes(),
        "links": [],
    }
    script = tmp_path / "rtl-buddy-view"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "if argv and argv[0] == '--version':\n"
        "    print('rtl-buddy-view 0.4.0')\n"
        "    sys.exit(0)\n"
        "out = argv[argv.index('--output') + 1]\n"
        "os.makedirs(os.path.dirname(out), exist_ok=True)\n"
        f"open(out, 'w').write({json.dumps(json.dumps(graph))})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _build(root: Path, view: Path, **kwargs):
    return build_graph(
        root,
        view_executable=str(view),
        view_version="0.4.0",
        graphify_enabled=False,
        **kwargs,
    )


def test_end_to_end_binds_the_cocotb_suite_to_the_dut(
    cocotb_project: Path, tmp_path: Path
):
    build = _build(cocotb_project, _fake_view(tmp_path))
    graph = json.loads(build.graph_path.read_text())
    links = graph["links"]
    node_ids = {n["id"] for n in graph["nodes"]}

    test_id = "test:verif/alu_cocotb#cocotb_basic"
    entry = PY_NODE_PREFIX + "verif/alu_cocotb/test_alu.py"
    helper = PY_NODE_PREFIX + "verif/alu_cocotb/_alu_common.py"
    assert {entry, helper} <= node_ids

    # Acceptance: the test reaches the DUT module in two hops.
    binds = {(x["source"], x["target"]) for x in links if x["type"] == "binds_to"}
    assert (test_id, entry) in binds
    assert (entry, "module:alu") in binds

    # Acceptance: dut.a / dut.b / dut.op resolve to real port nodes, even
    # though only the helper touches them.
    drives = [x for x in links if x["type"] == "drives"]
    for signal in ("a", "b", "op"):
        target = f"port:alu.{signal}"
        assert target in node_ids
        hits = [x for x in drives if x["target"] == target]
        assert hits and all(x["confidence"] == "EXTRACTED" for x in hits)
        assert any(x["source"] == entry for x in hits)

    # Acceptance: the golden model the suite scoreboards against, reached
    # through the helper's sys.path insert.
    checks = [x for x in links if x["type"] == "checks_against"]
    assert [(x["source"], x["target"]) for x in checks] == [
        (test_id, "golden:spec/alu/alu_model.py")
    ]

    assert build.binding["status"] == "built"
    assert build.binding["tests"] == 1
    assert build.binding["drives_extracted"] == build.binding["drives"]
    assert build.binding["checks_against"] == 1


def test_binding_stage_is_recorded_in_the_meta_sidecar_and_its_own_file(
    cocotb_project: Path, tmp_path: Path
):
    build = _build(cocotb_project, _fake_view(tmp_path))
    meta = json.loads(build.meta_path.read_text())
    assert meta["binding"]["status"] == "built"
    assert meta["binding"]["python_modules"] == 2

    # The stage's own contribution is kept beside the merged file, apart
    # from Graphify's `binding/graph.json`, so a surprise is traceable.
    own = build.graph_path.parent / "bind" / "graph.json"
    assert own.is_file()
    payload = json.loads(own.read_text())
    assert {n["tier"] for n in payload["nodes"]} == {"binding"}


def test_no_bind_leaves_the_merged_graph_without_binding_edges(
    cocotb_project: Path, tmp_path: Path
):
    build = _build(cocotb_project, _fake_view(tmp_path), bind=False)
    graph = json.loads(build.graph_path.read_text())
    assert not [
        x
        for x in graph["links"]
        if x["type"] in ("binds_to", "drives", "checks_against", "imports")
    ]
    assert build.binding["status"] == "skipped"
    assert not (build.graph_path.parent / "bind").exists()


def test_editing_a_cocotb_module_invalidates_the_cache_without_graphify(
    cocotb_project: Path, tmp_path: Path
):
    view = _fake_view(tmp_path)
    first = _build(cocotb_project, view)
    assert _build(cocotb_project, view).unchanged is True

    # The stage reads verif Python whether or not Graphify is installed,
    # so those files have to be in the fingerprint.
    module = cocotb_project / "verif" / "alu_cocotb" / "test_alu.py"
    module.write_text(module.read_text() + "    dut.zf.value = 0\n")
    second = _build(cocotb_project, view)
    assert second.unchanged is False
    assert second.fingerprint != first.fingerprint


def _fake_graphify(tmp_path: Path) -> Path:
    """Stub ``graphify`` claiming the cocotb module under its own id.

    The point of the stub is the ``file`` attribute: that is the only
    thing the two tools agree on, and it is what the binding stage keys
    the hand-off on.
    """
    graph = {
        "directed": True,
        "multigraph": True,
        "graph": {
            "schema_version": 1,
            "generator": {"tool": "graphify", "version": "1.2.3", "tier": "binding"},
        },
        "nodes": [
            {
                "id": "pymod:verif/alu_cocotb/test_alu.py",
                "type": "python_module",
                "label": "test_alu",
                "tier": "binding",
                "file": "verif/alu_cocotb/test_alu.py",
            }
        ],
        "links": [],
    }
    script = tmp_path / "graphify"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "if argv and argv[0] == '--version':\n"
        "    print('graphify 1.2.3')\n"
        "    sys.exit(0)\n"
        "out = argv[argv.index('--output') + 1]\n"
        "os.makedirs(os.path.dirname(out) or '.', exist_ok=True)\n"
        "if argv[0] == 'extract':\n"
        f"    open(out, 'w').write({json.dumps(json.dumps(graph))})\n"
        "    sys.exit(0)\n"
        "nodes = {}\n"
        "for path in [a for a in argv[1:] if a.endswith('.json') and a != out]:\n"
        "    with open(path) as fh:\n"
        "        for n in json.load(fh).get('nodes') or []:\n"
        "            nodes.setdefault(n['id'], n)\n"
        "json.dump({'directed': True, 'multigraph': True, 'graph': {},\n"
        "           'nodes': list(nodes.values()), 'links': []}, open(out, 'w'))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_graphify_and_the_binding_stage_share_one_node_per_file(
    cocotb_project: Path, tmp_path: Path
):
    gfy = _fake_graphify(tmp_path)
    build = build_graph(
        cocotb_project,
        view_executable=str(_fake_view(tmp_path)),
        view_version="0.4.0",
        graphify_executable=str(gfy),
        graphify_version="1.2.3",
    )
    graph = json.loads(build.graph_path.read_text())
    node_ids = {n["id"] for n in graph["nodes"]}

    # Graphify's id is adopted; no second node for the same file.
    assert "pymod:verif/alu_cocotb/test_alu.py" in node_ids
    assert PY_NODE_PREFIX + "verif/alu_cocotb/test_alu.py" not in node_ids
    assert build.binding["reused_node_ids"] == 1
    binds = {
        (x["source"], x["target"]) for x in graph["links"] if x["type"] == "binds_to"
    }
    assert ("pymod:verif/alu_cocotb/test_alu.py", "module:alu") in binds

    # The binding tier has two producers but is still one tier, and the
    # cross-check sees the stage's file too, so it must still agree.
    assert build.merge["tiers"].count("binding") == 1
    assert build.merge["graphify_cross_check"]["status"] == "ok"


def test_machine_envelope_carries_the_binding_block(
    cocotb_project: Path, tmp_path: Path
):
    view = _fake_view(tmp_path)
    result = CliRunner().invoke(
        RtlBuddy(name="test_graph_binding").app,
        ["--machine", "graph", "build", "--tool", str(view), "--no-graphify"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["payload"]
    assert payload["binding"]["status"] == "built"
    assert payload["binding"]["tests"] == 1


def test_no_bind_flag_is_wired_through(cocotb_project: Path, tmp_path: Path):
    view = _fake_view(tmp_path)
    result = CliRunner().invoke(
        RtlBuddy(name="test_graph_binding").app,
        [
            "--machine",
            "graph",
            "build",
            "--tool",
            str(view),
            "--no-graphify",
            "--no-bind",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["payload"]["binding"]["status"] == "skipped"
