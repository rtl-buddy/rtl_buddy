"""Correctness smoke for the token benchmark (#381).

The benchmark's *numbers* are not a test — they depend on which project
you point it at, and a threshold on them would fail for every reason
except the one worth failing for. What is worth guarding is the part
that makes the numbers mean anything:

* the token proxy is the documented one (`len(text) // 4`, on the
  command as well as the output),
* the hand-checked key is well-formed and every task carries one,
* the raw route's small parsers — the only place the benchmark could
  quietly hand one route a wrong answer — still read SystemVerilog
  headers, instance bindings, `tests.yaml` and `specs.yaml` correctly.

The end-to-end run over a real project is the last test here and skips
unless `RTL_BUDDY_TEMPLATE_ROOT` points at a checkout that has been
through `rb graph build`. That is deliberate: CI has no
rtl-buddy-project-template checkout and no `rtl-buddy-view`, so the
alternative to a skip is a red suite that means nothing.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "graph_token_benchmark.py"


def _load_benchmark():
    """Import scripts/graph_token_benchmark.py — it is a script, not a package.

    It has to land in ``sys.modules`` *before* it executes: its
    dataclasses resolve their own annotations through the module entry,
    and a module that is not registered yet has no entry to resolve
    against.
    """
    spec = importlib.util.spec_from_file_location("graph_token_benchmark", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bench = _load_benchmark()


# ---------------------------------------------------------------------------
# the proxy and the task set
# ---------------------------------------------------------------------------


def test_token_proxy_is_chars_over_four():
    assert bench.CHARS_PER_TOKEN == 4
    assert bench.approx_tokens("") == 0
    assert bench.approx_tokens("abcd") == 1
    assert bench.approx_tokens("a" * 4000) == 1000


def test_repeating_a_query_is_free_the_way_re_reading_a_file_is(tmp_path):
    """The change-impact walk revisits its own starting node.

    Charging the same `explain` twice would tax the graph route for a
    line an agent already has in its transcript — the mirror of the rule
    `read()` has always followed.
    """
    stub = "import json; print(json.dumps({'payload': {'node': {}}}))"
    route = bench.Route(
        bench.Runner(project=tmp_path, rb=[sys.executable, "-c", stub]), "graph"
    )
    first = route.machine("graph", "explain", "module:x")
    second = route.machine("graph", "explain", "module:x")
    assert first == second
    assert route.run.calls == 1
    route.machine("graph", "explain", "module:y")
    assert route.run.calls == 2


def test_a_step_charges_for_the_command_as_well_as_the_output():
    step = bench.Step("cat verif/x/tests.yaml", "y" * 400)
    assert step.tokens == bench.approx_tokens(step.command) + 100


def test_every_task_has_a_key_a_question_and_an_expected_answer():
    assert len(bench.TASKS) == 6
    keys = [task.key for task in bench.TASKS]
    assert len(set(keys)) == len(keys)
    for task in bench.TASKS:
        assert task.question.strip()
        assert task.expected
        assert callable(task.graph) and callable(task.raw)
        assert bench.answer_floor(task) > 0


def test_the_answer_floor_is_smaller_than_any_route_could_be():
    # A sanity rail on the third column: the answer serialized as JSON
    # has to be small, or it is not a floor, it is a route.
    for task in bench.TASKS:
        assert bench.answer_floor(task) < 200


# ---------------------------------------------------------------------------
# the raw route's parsers
# ---------------------------------------------------------------------------

_SV = """\
// a comment mentioning input fake_port
module widget #(
  parameter int W = 8,
  parameter bit EN = 1'b1
)(
  input  logic         clk,
  input  logic [W-1:0] d,     // data in
  output logic         q
);
  child_thing #(.W(W)) u_child (
    .clk(clk),
    .rst_n(rst_local),
    .out(q)
  );
  other_thing u_other (.clk(clk), .rst_n(rst_local));
endmodule
"""


def test_module_ports_reads_directions_and_ignores_comments():
    ports = bench._module_ports(_SV)["widget"]
    assert [(name, direction) for name, direction, _t in ports] == [
        ("clk", "input"),
        ("d", "input"),
        ("q", "output"),
    ]


def test_module_params_reads_the_parameter_list():
    assert bench._module_params(_SV)["widget"] == ["W", "EN"]


def test_instance_bindings_finds_every_load_of_a_net():
    bindings = bench._instance_bindings(_SV, "rst_local")
    assert sorted(bindings) == [
        ("child_thing", "u_child", "rst_n"),
        ("other_thing", "u_other", "rst_n"),
    ]


def test_instance_bindings_does_not_confuse_a_different_net():
    assert bench._instance_bindings(_SV, "rst_n") == []


_TESTS_YAML = """\
rtl-buddy-filetype: test_config
testbenches:
  - name: "tb_top"
    toplevel: "tb_top"
tests:
  - name: "basic"
    reglvl: 0
    model: "widget"
    testbench: "tb_top"
    covers:
      - "BLK-FUNC-A"
      - "BLK-FUNC-B"
  - name: "nightly"
    reglvl: 1000  # deferred to the full regression
    model: "widget_subsys"
    testbench: "tb_top"
"""


def test_tests_yaml_parse_keeps_model_reglvl_and_covers():
    entries = bench._tests_with_covers(_TESTS_YAML)
    assert [(name, model, reglvl) for name, model, reglvl, _c in entries] == [
        ("basic", "widget", 0),
        ("nightly", "widget_subsys", 1000),
    ]
    assert entries[0][3] == ["BLK-FUNC-A", "BLK-FUNC-B"]
    assert entries[1][3] == []


_SPECS_YAML = """\
rtl-buddy-filetype: spec_config
blocks:
  - name: "widget"
    desc: "a widget"
    docs:
      - "README.md"
    coverage-items:
      - id: "BLK-FUNC-A"
        desc: "does A"
      - id: "BLK-FUNC-B"
        desc: "does B"
"""


def test_specs_yaml_parse_reads_block_docs_and_items():
    blocks = bench._spec_blocks(_SPECS_YAML)
    assert blocks == [("widget", ["README.md"], ["BLK-FUNC-A", "BLK-FUNC-B"])]


def test_test_records_carry_the_testbench_the_deep_chain_hops_through():
    records = {r["name"]: r for r in bench._test_records(_TESTS_YAML)}
    assert records["basic"]["testbench"] == "tb_top"
    assert records["basic"]["covers"] == ["BLK-FUNC-A", "BLK-FUNC-B"]
    assert records["nightly"]["model"] == "widget_subsys"


# ---------------------------------------------------------------------------
# the parsers the two structural tasks added
# ---------------------------------------------------------------------------

_SV_TREE = """\
// widget_top wraps widget. The comment below is the false positive the
// change-impact raw route pays to read: ip_cdc_sync is named, not used.
module widget_top (input logic clk);
  widget u_w (.clk(clk));
  leaf   u_l (.clk(clk));
endmodule

module widget (input logic clk);
  leaf u_leaf (.clk(clk));
endmodule
"""


def test_module_instantiations_maps_each_module_to_its_children():
    assert bench._module_instantiations(_SV_TREE) == {
        "widget_top": {"widget", "leaf"},
        "widget": {"leaf"},
    }


def test_module_instantiations_ignores_a_name_that_only_appears_in_a_comment():
    # The whole point of the fixpoint being over parsed instances rather
    # than over grep hits: a mention is not a consumer.
    for children in bench._module_instantiations(_SV_TREE).values():
        assert "ip_cdc_sync" not in children


_SYNTH_YAML = """\
rtl-buddy-filetype: synth_config
syntheses:
  - name: "widget_synth_generic"
    model: "widget_synth_top"
    tool: "yosys"
    reglvl: 0
  - name: "widget_synth_nangate45"
    model: "widget_synth_top"
    reglvl: 1000
"""


def test_runs_in_yaml_reads_a_synthesis_suite_not_only_a_test_suite():
    assert bench._runs_in_yaml(_SYNTH_YAML) == [
        ("widget_synth_generic", "widget_synth_top"),
        ("widget_synth_nangate45", "widget_synth_top"),
    ]


def test_runs_in_yaml_still_reads_a_tests_yaml():
    assert bench._runs_in_yaml(_TESTS_YAML) == [
        ("basic", "widget"),
        ("nightly", "widget_subsys"),
    ]


_MODELS_YAML = """\
rtl-buddy-filetype: model_config
models:
  - name: "widget"
    desc: "a widget"
    filelist: ["-v widget.sv"]
    spec: "../../spec/widget/specs.yaml"
  - name: "widget_subsys"
    filelist: ["-F widget_subsys.f"]
"""


def test_model_entries_reads_names_and_spec_paths():
    assert bench._model_entries(_MODELS_YAML) == {
        "widget": "../../spec/widget/specs.yaml",
        "widget_subsys": None,
    }


_MANIFEST = """\
rtl-buddy-filetype: reg_config
# Discoverable list of suites.
test-configs:
  - "verif/apb/tests.yaml"
  - "verif/widget/tests.yaml"   # the leaf IP
"""


def test_manifest_entries_lists_the_suite_configs_a_flow_claims():
    assert bench._manifest_entries(_MANIFEST) == [
        "verif/apb/tests.yaml",
        "verif/widget/tests.yaml",
    ]


def test_elaboration_root_survives_the_suite_qualification():
    assert (
        bench._elaboration_root("inst:mem_subsys/mem_subsys.u_wr_hs.u_sync_ack")
        == "module:mem_subsys"
    )
    assert (
        bench._elaboration_root("inst:tb_top/tb_top.u_dut@verif/ip_cdc_sync")
        == "module:tb_top@verif/ip_cdc_sync"
    )
    assert bench._elaboration_root("inst:ip_cdc_sync/ip_cdc_sync") == (
        "module:ip_cdc_sync"
    )


# ---------------------------------------------------------------------------
# end to end, when a real project is available
# ---------------------------------------------------------------------------

_TEMPLATE = os.environ.get("RTL_BUDDY_TEMPLATE_ROOT")
_HAS_GRAPH = bool(_TEMPLATE) and (Path(_TEMPLATE) / bench.GRAPH_JSON).exists()

pytestmark_reason = (
    "set RTL_BUDDY_TEMPLATE_ROOT to a project that has been through `rb graph build`"
)


@pytest.mark.skipif(not _HAS_GRAPH, reason=pytestmark_reason)
@pytest.mark.parametrize("task", bench.TASKS, ids=lambda t: t.key)
def test_both_routes_answer_correctly(task):
    """Correctness only — no token count is asserted, by design."""
    runner = bench.Runner(
        project=Path(_TEMPLATE).resolve(), rb=[sys.executable, "-m", "rtl_buddy"]
    )
    result = bench.run_task(runner, task)
    for route in ("graph", "raw"):
        assert result[route]["error"] is None, result[route]["error"]
        assert result[route]["answer"] == task.expected, (
            f"{route} route answered {result[route]['answer']!r}"
        )
