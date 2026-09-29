#!/usr/bin/env python3
"""Token-efficiency benchmark: graph queries vs raw-file context.

Six questions an agent asks about an RTL project are answered twice: through
`rb --machine graph query|path|explain` against `artefacts/graph/graph.json`,
and through filelist, grep and whole-file reads. Each answer is checked against
a hand-written key (`EXPECTED_*`); a wrong route is reported as wrong.

Tokens are estimated as `len(text) // 4` over the commands typed plus the bytes
read back. `rb graph build` is not counted because the graph is built once per
source change.

Usage:
    uv run python scripts/graph_token_benchmark.py --project /path/to/rtl-buddy-project-template
    uv run python scripts/graph_token_benchmark.py -p ... --markdown   # docs table
    uv run python scripts/graph_token_benchmark.py -p ... --json       # machine

The project needs `rb graph build` (all tiers) and `rb graph results` first.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

CHARS_PER_TOKEN = 4

#: Where the graph route reads from, relative to the project root.
GRAPH_JSON = Path("artefacts/graph/graph.json")


def approx_tokens(text: str) -> int:
    """Estimate tokens as four characters to a token, rounded down."""
    return len(text) // CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# route plumbing
# ---------------------------------------------------------------------------


@dataclass
class Step:
    """One command and the output read back."""

    command: str
    output: str

    @property
    def tokens(self) -> int:
        return approx_tokens(self.command) + approx_tokens(self.output)


@dataclass
class RouteRun:
    route: str
    steps: list[Step] = field(default_factory=list)
    answer: dict | None = None
    error: str | None = None

    @property
    def calls(self) -> int:
        return len(self.steps)

    @property
    def tokens(self) -> int:
        return sum(step.tokens for step in self.steps)

    @property
    def chars(self) -> int:
        return sum(len(s.command) + len(s.output) for s in self.steps)


class Route:
    """Records every command a route runs and its output.

    Routes may derive an answer only from text returned here.
    """

    def __init__(self, runner: Runner, name: str) -> None:
        self.runner = runner
        self.run = RouteRun(route=name)
        self._read: dict[str, str] = {}
        self._asked: dict[str, dict] = {}

    # -- graph route -------------------------------------------------
    def machine(self, *args: str) -> dict:
        """Run `rb --machine <args>` and return the payload.

        A repeated command is not charged again.
        """
        printed = "rb --machine " + " ".join(_quote(a) for a in args)
        if printed in self._asked:
            return self._asked[printed]
        argv = [*self.runner.rb, "--machine", *args]
        proc = subprocess.run(
            argv,
            cwd=self.runner.project,
            capture_output=True,
            text=True,
            check=False,
        )
        out = proc.stdout.strip()
        self.run.steps.append(Step(printed, out))
        if not out:
            raise RouteError(f"{printed}: no output (exit {proc.returncode})")
        envelope = json.loads(out)
        payload = envelope.get("payload") or {}
        self._asked[printed] = payload
        return payload

    # -- raw route ---------------------------------------------------
    def shell(self, argv: list[str], *, allow_fail: bool = True) -> str:
        """Run a shell command in the project and return its stdout."""
        proc = subprocess.run(
            argv,
            cwd=self.runner.project,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode not in (0, 1) and not allow_fail:
            raise RouteError(f"{' '.join(argv)} failed: {proc.stderr.strip()}")
        out = proc.stdout
        self.run.steps.append(Step(" ".join(_quote(a) for a in argv), out))
        return out

    def read(self, rel: str) -> str:
        """Read a whole file. A repeated read is not charged again."""
        if rel in self._read:
            return self._read[rel]
        text = (self.runner.project / rel).read_text()
        self._read[rel] = text
        self.run.steps.append(Step(f"cat {rel}", text))
        return text

    def read_lines(self, rel: str, start: int, end: int) -> str:
        """Read lines ``start`` to ``end`` (1-based, inclusive) of a file."""
        lines = (self.runner.project / rel).read_text().splitlines(keepends=True)
        text = "".join(lines[start - 1 : end])
        self.run.steps.append(Step(f"sed -n '{start},{end}p' {rel}", text))
        return text


class RouteError(RuntimeError):
    pass


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


@dataclass
class Runner:
    project: Path
    rb: list[str]


# ---------------------------------------------------------------------------
# task 1: trace a signal, driver and loads across the hierarchy
# ---------------------------------------------------------------------------
#
# "In demo_cdc_open_top, what drives rst_b_n and which instances load it?"
# The key is checked against design/demo_cdc_open/demo_cdc_open_top.sv.

TRACE_TOP = "demo_cdc_open_top"
TRACE_SIGNAL = "rst_b_n"
TOP_INSTANCE = f"inst:{TRACE_TOP}/{TRACE_TOP}"


def trace_signal_graph(route: Route) -> dict:
    """Explain the parent, each child, and each port hit.

    An internal net has no node; it appears only as the `actual` on a
    `connects` edge. Only `explain` reports a port's `dir`.
    """
    top = route.machine("graph", "explain", TOP_INSTANCE, "--no-results")
    children = [
        edge["peer"]
        for edge in top.get("incoming", [])
        if edge.get("type") == "child_of"
    ]

    hits: list[tuple[str, str, str]] = []  # (instance path, formal, port id)
    for child in sorted(children):
        payload = route.machine("graph", "explain", child, "--no-results")
        inst_path = payload["attributes"].get("instance_path", child)
        for edge in payload.get("outgoing", []):
            if edge.get("type") == "connects" and edge.get("actual") == TRACE_SIGNAL:
                hits.append((inst_path, edge["formal"], edge["peer"]))

    directions: dict[str, str] = {}
    for port_id in sorted({port for _inst, _formal, port in hits}):
        payload = route.machine("graph", "explain", port_id, "--no-results")
        directions[port_id] = payload["attributes"].get("dir", "?")

    return _trace_answer(
        [(inst, formal, directions.get(port, "?")) for inst, formal, port in hits]
    )


def trace_signal_raw(route: Route) -> dict:
    """grep for the net, read the parent and every child module it binds."""
    route.shell(["grep", "-rn", TRACE_SIGNAL, "design"])
    top_file = f"design/demo_cdc_open/{TRACE_TOP}.sv"
    top_text = route.read(top_file)

    hits = []
    for module, inst, formal in _instance_bindings(top_text, TRACE_SIGNAL):
        hits.append((module, inst, formal))

    modules = sorted({module for module, _inst, _formal in hits})
    grep = route.shell(["grep", "-rln", "-e", r"^module", "design/demo_cdc_open"])
    files = [line for line in grep.splitlines() if line.strip()]
    dirs: dict[tuple[str, str], str] = {}
    for path in files:
        text = route.read(path)
        for mod, ports in _module_ports(text).items():
            if mod in modules:
                for name, direction, _type_text in ports:
                    dirs[(mod, name)] = direction

    return _trace_answer(
        [
            (f"{TRACE_TOP}.{inst}", formal, dirs.get((module, formal), "?"))
            for module, inst, formal in hits
        ]
    )


def _trace_answer(hits: list[tuple[str, str, str]]) -> dict:
    drivers = sorted(f"{inst}.{formal}" for inst, formal, d in hits if d == "output")
    loads = sorted(f"{inst}.{formal}" for inst, formal, d in hits if d == "input")
    return {"driver": drivers, "loads": loads}


# ---------------------------------------------------------------------------
# task 2: which tests exercise block X, at which reglvl
# ---------------------------------------------------------------------------
#
# The key is checked against verif/*/tests.yaml. `demo_tiny_alu_subsys` runs
# model `demo_tiny_alu_subsys_top` and is not an answer: a name-substring trap.

BLOCK = "demo_tiny_alu"
MODEL_NODE = f"model:design/{BLOCK}/models.yaml#{BLOCK}"


def tests_for_block_graph(route: Route) -> dict:
    """Walk model -> testbenches (`exercises`) -> tests (`runs_on`) -> reglvl.

    Starts from the model node because a keyword search would also match the subsys tests.
    """
    model = route.machine("graph", "explain", MODEL_NODE, "--no-results")
    benches = [
        edge["peer"]
        for edge in model.get("incoming", [])
        if edge.get("type") == "exercises"
    ]

    tests: list[str] = []
    for bench in sorted(benches):
        payload = route.machine("graph", "explain", bench, "--no-results")
        tests += [
            edge["peer"]
            for edge in payload.get("incoming", [])
            if edge.get("type") == "runs_on"
        ]

    answer: dict[str, object] = {}
    for test in sorted(set(tests)):
        payload = route.machine("graph", "explain", test, "--no-results")
        answer[test[len("test:") :]] = payload["attributes"].get("reglvl")
    return {"tests": answer}


def tests_for_block_raw(route: Route) -> dict:
    """grep the verif tree for the model name and read every suite that hits."""
    grep = route.shell(["grep", "-rl", BLOCK, "--include=tests.yaml", "verif"])
    answer: dict[str, object] = {}
    for path in sorted(line for line in grep.splitlines() if line.strip()):
        text = route.read(path)
        suite = str(Path(path).parent).replace(os.sep, "/")
        for name, model, reglvl in _tests_in_yaml(text):
            if model == BLOCK:
                answer[f"{suite}#{name}"] = reglvl
    return {"tests": answer}


# ---------------------------------------------------------------------------
# task 3: test -> coverage item -> spec doc -> golden model
# ---------------------------------------------------------------------------
#
# The key is checked against spec/demo_tiny_alu/specs.yaml, the block README,
# and the two tests.yaml files that claim the item.

COVITEM = "SAND-FUNC-FLAG-C-ADD"


def traceability_graph(route: Route) -> dict:
    """`query` the item, then `explain` the block that declares it.

    Depth 1: at depth 2 the block's other coverage items exhaust the
    neighbour budget before the doc and golden model are reached.
    """
    payload = route.machine(
        "graph", "query", f"which tests cover {COVITEM}", "--depth", "1", "--no-results"
    )
    match = next(
        (m for m in payload.get("matches", []) if m.get("type") == "coverage_item"),
        None,
    )
    if match is None:
        raise RouteError(f"no coverage_item matched {COVITEM}")
    if match.get("neighbors_truncated"):
        raise RouteError("neighbourhood truncated — the answer may be incomplete")

    tests, blocks, docs, goldens = [], [], [], []
    block_ids = []
    for neighbor in match.get("neighbors", []):
        kind = neighbor.get("type")
        if kind == "test" and neighbor["via"]["type"] == "covers":
            tests.append(neighbor["id"][len("test:") :])
        elif kind == "spec_block":
            blocks.append(neighbor["label"])
            block_ids.append(neighbor["id"])

    for block_id in sorted(set(block_ids)):
        block = route.machine("graph", "explain", block_id, "--no-results")
        docs += [
            edge["peer"][len("doc:") :]
            for edge in block.get("outgoing", [])
            if edge.get("type") == "documented_by"
        ]
        goldens += [
            edge["peer"][len("golden:") :]
            for edge in block.get("incoming", [])
            if edge.get("type") == "implements"
        ]
    return _trace_chain(tests, blocks, docs, goldens)


def traceability_raw(route: Route) -> dict:
    """grep the id, then read the suites that claim it and the spec that declares it."""
    grep = route.shell(["grep", "-rn", COVITEM, "verif", "spec"])
    tests: list[str] = []
    spec_files: list[str] = []
    for line in grep.splitlines():
        path = line.split(":", 1)[0]
        if path.endswith("tests.yaml"):
            suite = str(Path(path).parent).replace(os.sep, "/")
            for name, _model, _reglvl, covers in _tests_with_covers(route.read(path)):
                if COVITEM in covers:
                    tests.append(f"{suite}#{name}")
        elif path.endswith("specs.yaml") and path not in spec_files:
            spec_files.append(path)

    blocks, docs, goldens = [], [], []
    for path in spec_files:
        text = route.read(path)
        spec_dir = str(Path(path).parent).replace(os.sep, "/")
        for block, block_docs, items in _spec_blocks(text):
            if COVITEM in items:
                blocks.append(block)
                docs += [f"{spec_dir}/{d}" for d in block_docs]
        listing = route.shell(["ls", spec_dir])
        goldens += [
            f"{spec_dir}/{name}"
            for name in listing.split()
            if name.endswith(".py") and not name.startswith("_")
        ]
    return _trace_chain(tests, blocks, docs, goldens)


def _trace_chain(
    tests: list[str], blocks: list[str], docs: list[str], goldens: list[str]
) -> dict:
    return {
        "tests": sorted(set(tests)),
        "blocks": sorted(set(blocks)),
        "docs": sorted(set(docs)),
        "goldens": sorted(set(goldens)),
    }


# ---------------------------------------------------------------------------
# task 4: summarize a module's interface
# ---------------------------------------------------------------------------
#
# The key is checked against design/demo_tiny_alu/demo_tiny_alu.sv: ten ports
# and one parameter, W.

IFACE_MODULE = "demo_tiny_alu"


def interface_graph(route: Route) -> dict:
    """Query the module's ports and parameters; a match carries the port's `dir`.

    `--limit 20` is deliberate. No edge ties a port to its module, so the
    search is by substring and the `demo_tiny_alu_subsys_*` ports score the
    same. The real ports sort first, so a limit above the expected count
    shows the list is complete.
    """
    ports = route.machine(
        "graph",
        "query",
        IFACE_MODULE,
        "--type",
        "port",
        "--depth",
        "0",
        "--no-results",
        "--limit",
        "20",
    )
    params = route.machine(
        "graph",
        "query",
        IFACE_MODULE,
        "--type",
        "parameter",
        "--depth",
        "0",
        "--no-results",
        "--limit",
        "20",
    )

    prefix = f"port:{IFACE_MODULE}."
    port_nodes = [m for m in ports.get("matches", []) if m["id"].startswith(prefix)]
    param_names = sorted(
        m["id"][len(f"param:{IFACE_MODULE}.") :]
        for m in params.get("matches", [])
        if m["id"].startswith(f"param:{IFACE_MODULE}.")
    )
    if not port_nodes:
        raise RouteError(f"no port nodes for {IFACE_MODULE}")

    return {
        "ports": sorted(
            f"{m['id'][len(prefix) :]}:{m.get('attributes', {}).get('dir', '?')}"
            for m in port_nodes
        ),
        "params": param_names,
    }


def interface_raw(route: Route) -> dict:
    """grep for the module declaration and read that file."""
    grep = route.shell(["grep", "-rn", f"^module {IFACE_MODULE}", "design"])
    files = sorted(
        {line.split(":", 1)[0] for line in grep.splitlines() if line.strip()}
    )
    if not files:
        raise RouteError(f"grep found no declaration of {IFACE_MODULE}")
    text = route.read(files[0])
    ports = _module_ports(text).get(IFACE_MODULE, [])
    return {
        "ports": sorted(f"{name}:{direction}" for name, direction, _t in ports),
        "params": sorted(_module_params(text).get(IFACE_MODULE, [])),
    }


# ---------------------------------------------------------------------------
# task 5: the deep chain, coverage item -> tests -> testbenches -> DUT -> spec
# ---------------------------------------------------------------------------
#
# Task 3 stops at the block that declares the item. This task follows the
# chain from a coverage hole to what checks it:
#
#   covitem:demo_tiny_alu#SAND-FUNC-OP-ADD
#     <- covers      test:verif/demo_tiny_alu#basic
#     <- covers      test:verif/demo_tiny_alu#ops_sweep
#     <- covers      test:verif/demo_tiny_alu_cocotb#cocotb_random
#     -> runs_on     tb:verif/demo_tiny_alu#tb_top          (SV testbench)
#     -> runs_on     tb:verif/demo_tiny_alu_cocotb#tb_alu_random  (cocotb)
#     -> exercises   model:design/demo_tiny_alu/models.yaml#demo_tiny_alu
#     -> maps_to     module:demo_tiny_alu  (design/demo_tiny_alu/demo_tiny_alu.sv)
#     -> specified_by spec:demo_tiny_alu
#     -> documented_by doc:spec/demo_tiny_alu/README.md
#     <- implements  golden:spec/demo_tiny_alu/tiny_alu_model.py
#
# The item spans two suites and two testbench kinds: an SV tb_top and a cocotb
# bench whose toplevel is the DUT.

DEEP_ITEM = "SAND-FUNC-OP-ADD"
DEEP_ITEM_NODE = f"covitem:demo_tiny_alu#{DEEP_ITEM}"


def deep_chain_graph(route: Route) -> dict:
    """`explain` each node on the chain, starting from the item's derivable node id."""
    item = route.machine("graph", "explain", DEEP_ITEM_NODE, "--no-results")
    tests = [
        edge["peer"]
        for edge in item.get("incoming", [])
        if edge.get("type") == "covers"
    ]
    if not tests:
        raise RouteError(f"nothing covers {DEEP_ITEM}")

    benches: list[str] = []
    for test in sorted(tests):
        payload = route.machine("graph", "explain", test, "--no-results")
        benches += [
            edge["peer"]
            for edge in payload.get("outgoing", [])
            if edge.get("type") == "runs_on"
        ]

    models: list[str] = []
    for bench in sorted(set(benches)):
        payload = route.machine("graph", "explain", bench, "--no-results")
        models += [
            edge["peer"]
            for edge in payload.get("outgoing", [])
            if edge.get("type") == "exercises"
        ]

    dut_modules: list[str] = []
    specs: list[str] = []
    for model in sorted(set(models)):
        payload = route.machine("graph", "explain", model, "--no-results")
        for edge in payload.get("outgoing", []):
            if edge.get("type") == "maps_to":
                dut_modules.append(edge["peer"])
            elif edge.get("type") == "specified_by":
                specs.append(edge["peer"])

    duts: list[tuple[str, str]] = []
    for module in sorted(set(dut_modules)):
        # Edges name the peer but not its file; explain the module node for it.
        payload = route.machine("graph", "explain", module, "--no-results")
        duts.append((payload["node"]["label"], payload["node"]["file"]))

    docs: list[str] = []
    goldens: list[str] = []
    for spec in sorted(set(specs)):
        payload = route.machine("graph", "explain", spec, "--no-results")
        docs += [
            edge["peer"][len("doc:") :]
            for edge in payload.get("outgoing", [])
            if edge.get("type") == "documented_by"
        ]
        goldens += [
            edge["peer"][len("golden:") :]
            for edge in payload.get("incoming", [])
            if edge.get("type") == "implements"
        ]
    return _deep_chain_answer(tests, benches, duts, docs, goldens)


def deep_chain_raw(route: Route) -> dict:
    """grep the id, then follow the chain through the files.

    The grep finds both the claiming suites and the declaring spec, so the
    route reads `specs.yaml` directly instead of going through `models.yaml`.
    """
    grep = route.shell(["grep", "-rn", DEEP_ITEM, "verif", "spec"])
    suite_files: list[str] = []
    spec_files: list[str] = []
    for line in grep.splitlines():
        path = line.split(":", 1)[0]
        if path.endswith("tests.yaml") and path not in suite_files:
            suite_files.append(path)
        elif path.endswith("specs.yaml") and path not in spec_files:
            spec_files.append(path)

    tests: list[str] = []
    benches: list[str] = []
    model_names: list[str] = []
    for path in sorted(suite_files):
        text = route.read(path)
        suite = str(Path(path).parent).replace(os.sep, "/")
        for record in _test_records(text):
            if DEEP_ITEM not in record["covers"]:
                continue
            tests.append(f"{suite}#{record['name']}")
            if record["testbench"]:
                benches.append(f"{suite}#{record['testbench']}")
            if record["model"]:
                model_names.append(record["model"])

    duts: list[tuple[str, str]] = []
    if model_names:
        # A model's name is its elaboration top, so its declaration names the file.
        argv = ["grep", "-rn"]
        for name in sorted(set(model_names)):
            argv += ["-e", f"^module {name}"]
        argv.append("design")
        for line in route.shell(argv).splitlines():
            if not line.strip():
                continue
            file, _, rest = line.partition(":")
            name = rest.split("module ", 1)[-1].split()[0].rstrip("#(;")
            if name in model_names:
                duts.append((name, file))

    docs: list[str] = []
    goldens: list[str] = []
    for path in sorted(spec_files):
        text = route.read(path)
        spec_dir = str(Path(path).parent).replace(os.sep, "/")
        for _block, block_docs, items in _spec_blocks(text):
            if DEEP_ITEM in items:
                docs += [f"{spec_dir}/{doc}" for doc in block_docs]
        listing = route.shell(["ls", spec_dir])
        goldens += [
            f"{spec_dir}/{name}"
            for name in listing.split()
            if name.endswith(".py") and not name.startswith("_")
        ]
    return _deep_chain_answer(tests, benches, duts, docs, goldens)


def _deep_chain_answer(
    tests: list[str],
    benches: list[str],
    duts: list[tuple[str, str]],
    docs: list[str],
    goldens: list[str],
) -> dict:
    return {
        "tests": sorted({_strip_prefix(t, "test:") for t in tests}),
        "testbenches": sorted({_strip_prefix(b, "tb:") for b in benches}),
        "dut": sorted({name for name, _file in duts}),
        "dut_file": sorted({file for _name, file in duts}),
        "docs": sorted(set(docs)),
        "goldens": sorted(set(goldens)),
    }


def _strip_prefix(value: str, prefix: str) -> str:
    return value[len(prefix) :] if value.startswith(prefix) else value


# ---------------------------------------------------------------------------
# task 6: change impact on IP that half the tree instantiates
# ---------------------------------------------------------------------------
#
# "`design/common/ip_cdc_sync.sv` changed — which runs must re-run?"
#
# A run counts when a project-root regression manifest (`regression.yaml`,
# `synth_regression.yaml`, `fpv_regression.yaml`, `fpga_regression.yaml`)
# claims its suite and the elaboration it drives contains an `ip_cdc_sync`
# instance. Synthesis of an affected top counts like simulation.
#
# The template's CDC analyses are out of scope for both routes:
# `cdc_regression.yaml` is at `lint/cdc/`, so graph flow discovery, which goes
# by root filename, never sees it. It would add `ip_cdc_handshake_lint`,
# `demo_tiny_alu_subsys_lint` and `demo_cdc_mem_macro_lint`.
#
# The key is checked against the sources. Direct instantiators are
# `ip_cdc_handshake`, `ip_async_fifo` and `demo_tiny_alu_subsys_top`; transitive
# ones are `mem_subsys` and `demo_tiny_alu_subsys_synth_top`.
# `demo_tiny_alu_subsys_compute` pulls only the ALU and is not affected.
# `cdc_open_sync.sv` names the IP only in a comment and is a false positive the
# raw route pays to read. `mem_subsys` is affected but no root manifest runs it.

IP_BLOCK = "ip_cdc_sync"
IP_MODULE = f"module:{IP_BLOCK}"

#: Project-root manifests that define which runs exist.
RUN_MANIFESTS = (
    "regression.yaml",
    "synth_regression.yaml",
    "fpv_regression.yaml",
    "fpga_regression.yaml",
)


def change_impact_graph(route: Route) -> dict:
    """Read the transitive closure from the module's `instance_of` edges.

    Elaboration flattens the hierarchy, so every instance hangs off the module
    node and the first component of an instance id is its elaboration root.
    Each root is then explained for its `maps_to` model, `elaborates_as`
    testbench or `targets` run, and each testbench for `runs_on`. Runs are read
    from the module because a cocotb bench elaborates as the DUT itself.
    """
    hub = route.machine("graph", "explain", IP_MODULE, "--no-results")
    roots = {
        _elaboration_root(edge["peer"])
        for edge in hub.get("incoming", [])
        if edge.get("type") == "instance_of"
    }
    if not roots:
        raise RouteError(f"no instance of {IP_BLOCK} in the graph")

    models: list[str] = []
    benches: list[str] = []
    runs: list[str] = []
    for root in sorted(roots):
        payload = route.machine("graph", "explain", root, "--no-results")
        for edge in payload.get("incoming", []):
            kind = edge.get("type")
            if kind == "maps_to":
                models.append(edge["peer"].split("#", 1)[-1])
            elif kind == "elaborates_as":
                benches.append(edge["peer"])
            elif kind == "targets":
                runs.append(_strip_prefix(edge["peer"], "test:"))

    for bench in sorted(set(benches)):
        payload = route.machine("graph", "explain", bench, "--no-results")
        runs += [
            _strip_prefix(edge["peer"], "test:")
            for edge in payload.get("incoming", [])
            if edge.get("type") == "runs_on"
        ]
    return {"models": sorted(set(models)), "runs": sorted(set(runs))}


def _elaboration_root(instance_id: str) -> str:
    """Map `inst:<root>/<path>[@<suite>]` to `module:<root>[@<suite>]`."""
    body, sep, suite = instance_id.partition("@")
    root = _strip_prefix(body, "inst:").split("/", 1)[0]
    return f"module:{root}" + (f"{sep}{suite}" if sep else "")


def change_impact_raw(route: Route) -> dict:
    """Grep to a fixpoint over the RTL, then read the manifests and suites.

    Each round greps for the newly found module names and opens the files that
    match, until a round adds no module.
    """
    closure = {IP_BLOCK}
    frontier = [IP_BLOCK]
    seen: set[str] = set()
    instantiates: dict[str, set[str]] = {}
    file_of: dict[str, str] = {}

    while frontier:
        argv = ["grep", "-rl", "--include=*.sv"]
        for name in sorted(frontier):
            argv += ["-e", name]
        argv.append("design")
        for path in sorted(
            line for line in route.shell(argv).splitlines() if line.strip()
        ):
            if path in seen:
                continue
            seen.add(path)
            text = route.read(path)
            for module, children in _module_instantiations(text).items():
                instantiates[module] = children
                file_of[module] = path
        frontier = sorted(
            module
            for module, children in instantiates.items()
            if module not in closure and children & closure
        )
        closure.update(frontier)

    # Only models.yaml says which modules are models.
    models: list[str] = []
    for design_dir in sorted(
        {str(Path(file_of[m]).parent).replace(os.sep, "/") for m in closure}
    ):
        text = route.read(f"{design_dir}/models.yaml")
        models += [name for name in _model_entries(text) if name in closure]

    suite_files: list[str] = []
    for manifest in RUN_MANIFESTS:
        suite_files += _manifest_entries(route.read(manifest))

    runs: list[str] = []
    if models and suite_files:
        argv = ["grep", "-l"]
        for name in sorted(set(models)):
            argv += ["-e", name]
        argv += sorted(set(suite_files))
        for path in sorted(
            line for line in route.shell(argv).splitlines() if line.strip()
        ):
            suite = str(Path(path).parent).replace(os.sep, "/")
            for name, model in _runs_in_yaml(route.read(path)):
                if model in models:
                    runs.append(f"{suite}#{name}")
    return {"models": sorted(set(models)), "runs": sorted(set(runs))}


# ---------------------------------------------------------------------------
# SystemVerilog and YAML parsing for the raw route
# ---------------------------------------------------------------------------
#
# Regex-thin on purpose: the raw route is charged for the bytes it reads, not
# for interpreting them.

_COMMENT = re.compile(r"//[^\n]*")
_MODULE_HEADER = re.compile(
    r"^module\s+(\w+)\s*(#\s*\((?P<params>.*?)\))?\s*\((?P<ports>.*?)\)\s*;",
    re.MULTILINE | re.DOTALL,
)
_PARAM = re.compile(r"\bparameter\s+(?:type\s+)?(?:[\w\[\]\-:'\s]*?\s)?(\w+)\s*=")
_INSTANCE = re.compile(
    r"^\s{0,6}(?P<module>[a-zA-Z_]\w*)\s*(#\s*\(.*?\)\s*)?(?P<inst>u_\w+)\s*\(",
    re.MULTILINE | re.DOTALL,
)
_BINDING = re.compile(r"\.(?P<formal>\w+)\s*\(\s*(?P<actual>[^()]*?)\s*\)")


def _strip_comments(text: str) -> str:
    return _COMMENT.sub("", text)


def _module_ports(text: str) -> dict[str, list[tuple[str, str, str]]]:
    """Map module name to [(port, direction, declared type)]."""
    out: dict[str, list[tuple[str, str, str]]] = {}
    body = _strip_comments(text)
    for match in _MODULE_HEADER.finditer(body):
        out[match.group(1)] = _port_decls(match.group("ports"))
    return out


def _port_decls(port_text: str) -> list[tuple[str, str, str]]:
    decls: list[tuple[str, str, str]] = []
    direction = ""
    for chunk in _strip_comments(port_text).split(","):
        chunk = " ".join(chunk.split())
        if not chunk:
            continue
        head = chunk.split()[0]
        if head in ("input", "output", "inout"):
            direction = head
            chunk = chunk[len(head) :].strip()
        if not direction or not chunk:
            continue
        name = chunk.split()[-1].rstrip(");")
        if not name.isidentifier():
            continue
        type_text = " ".join(chunk.split()[:-1])
        decls.append((name, direction, type_text))
    return decls


def _module_params(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for match in _MODULE_HEADER.finditer(_strip_comments(text)):
        params = match.group("params") or ""
        out[match.group(1)] = _PARAM.findall(params)
    return out


def _module_instantiations(text: str) -> dict[str, set[str]]:
    """Map module name to the module names instantiated in its body.

    Assumes modules do not nest and instances use the `u_` prefix.
    """
    body = _strip_comments(text)
    out: dict[str, set[str]] = {}
    for match in _MODULE_HEADER.finditer(body):
        end = body.find("\nendmodule", match.end())
        chunk = body[match.end() : end if end != -1 else len(body)]
        out[match.group(1)] = {m.group("module") for m in _INSTANCE.finditer(chunk)}
    return out


def _instance_bindings(text: str, signal: str) -> list[tuple[str, str, str]]:
    """Return [(module, instance, formal)] for every port bound to `signal`."""
    body = _strip_comments(text)
    found: list[tuple[str, str, str]] = []
    for match in _INSTANCE.finditer(body):
        start = match.end()
        depth = 1
        index = start
        while index < len(body) and depth:
            if body[index] == "(":
                depth += 1
            elif body[index] == ")":
                depth -= 1
            index += 1
        for binding in _BINDING.finditer(body[start : index - 1]):
            if binding.group("actual") == signal:
                found.append(
                    (
                        match.group("module"),
                        match.group("inst"),
                        binding.group("formal"),
                    )
                )
    return found


_TEST_ENTRY = re.compile(r"^\s*-\s*name:\s*\"?(?P<name>[\w.\-]+)\"?", re.MULTILINE)


def _yaml_section(text: str, key: str) -> str:
    """Return the block under a top-level key such as `tests:` or `testbenches:`."""
    collecting = False
    buffer: list[str] = []
    for line in text.splitlines():
        if re.match(rf"^{key}:\s*$", line):
            collecting = True
            continue
        if collecting and re.match(r"^\S", line):
            break
        if collecting:
            buffer.append(line)
    return "\n".join(buffer)


def _entry_chunks(section: str) -> list[tuple[str, str]]:
    """Split a list of mappings into [(entry name, entry text)]."""
    chunks: list[tuple[str, str]] = []
    starts = [m.start() for m in _TEST_ENTRY.finditer(section)]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(section)
        chunk = section[start:end]
        chunks.append((_TEST_ENTRY.match(chunk).group("name"), chunk))
    return chunks


def _scalar(chunk: str, key: str) -> str | None:
    match = re.search(rf"^\s*{key}:\s*\"?([\w./\-]+)\"?", chunk, re.MULTILINE)
    return match.group(1) if match else None


def _test_records(text: str) -> list[dict]:
    """Parse every entry under `tests:` in a tests.yaml into a record."""
    if not text:
        return []
    records: list[dict] = []
    for name, chunk in _entry_chunks(_yaml_section(text, "tests")):
        reglvl = re.search(r"^\s*reglvl:\s*(\S+)", chunk, re.MULTILINE)
        value: object = None
        if reglvl:
            raw = reglvl.group(1).split("#")[0].strip()
            value = int(raw) if raw.lstrip("-").isdigit() else raw
        records.append(
            {
                "name": name,
                "model": _scalar(chunk, "model"),
                "model_path": _scalar(chunk, "model_path"),
                "testbench": _scalar(chunk, "testbench"),
                "reglvl": value,
                "covers": re.findall(
                    r"^\s*-\s*\"?([A-Z][\w\-]+)\"?\s*$", chunk, re.MULTILINE
                ),
            }
        )
    return records


def _tests_with_covers(text: str) -> list[tuple[str, str | None, object, list[str]]]:
    """Return [(test name, model, reglvl, covers)] from a tests.yaml."""
    return [
        (r["name"], r["model"], r["reglvl"], r["covers"]) for r in _test_records(text)
    ]


def _tests_in_yaml(text: str) -> list[tuple[str, str | None, object]]:
    return [
        (name, model, reglvl) for name, model, reglvl, _c in _tests_with_covers(text)
    ]


#: The list key a run lives under, per flow.
RUN_SECTIONS = ("tests", "syntheses", "verifications", "analyses", "runs")


def _runs_in_yaml(text: str) -> list[tuple[str, str | None]]:
    """Return [(run name, model)] from any flow's suite config."""
    runs: list[tuple[str, str | None]] = []
    for key in RUN_SECTIONS:
        for name, chunk in _entry_chunks(_yaml_section(text, key)):
            runs.append((name, _scalar(chunk, "model")))
    return runs


def _model_entries(text: str) -> dict[str, str | None]:
    """Map model name to its `spec:` path from a models.yaml."""
    return {
        name: _scalar(chunk, "spec")
        for name, chunk in _entry_chunks(_yaml_section(text, "models"))
    }


def _manifest_entries(text: str) -> list[str]:
    """Return the suite config paths a regression manifest lists."""
    return re.findall(r"^\s*-\s*\"?([\w./\-]+\.yaml)\"?", text, re.MULTILINE)


def _spec_blocks(text: str) -> list[tuple[str, list[str], list[str]]]:
    """Return [(block name, docs, coverage-item ids)] from a specs.yaml."""
    blocks: list[tuple[str, list[str], list[str]]] = []
    starts = [m.start() for m in re.finditer(r"^\s*-\s*name:", text, re.MULTILINE)]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        chunk = text[start:end]
        name = re.search(r"name:\s*\"?([\w.\-]+)\"?", chunk).group(1)
        docs = re.findall(r"^\s*-\s*\"?([\w.\-/]+\.md)\"?", chunk, re.MULTILINE)
        items = re.findall(r"^\s*-\s*id:\s*\"?([\w\-]+)\"?", chunk, re.MULTILINE)
        if items:
            blocks.append((name, docs, items))
    return blocks


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


@dataclass
class Task:
    key: str
    title: str
    question: str
    expected: dict
    graph: Callable[[Route], dict]
    raw: Callable[[Route], dict]


EXPECTED_TRACE = {
    "driver": ["demo_cdc_open_top.u_reset_sync_b.rst_n"],
    "loads": [
        "demo_cdc_open_top.u_flag_sync.rst_n",
        "demo_cdc_open_top.u_gray_bus.dst_rst_n",
        "demo_cdc_open_top.u_handshake.dst_rst_n",
    ],
}

EXPECTED_TESTS = {
    "tests": {
        "verif/demo_tiny_alu#basic": 0,
        "verif/demo_tiny_alu#flags": 0,
        "verif/demo_tiny_alu#ops_sweep": 0,
        "verif/demo_tiny_alu#random": 0,
        "verif/demo_tiny_alu_cocotb#cocotb_flags": 1000,
        "verif/demo_tiny_alu_cocotb#cocotb_random": 1000,
        "verif/demo_tiny_alu_sc#basic_sc": 0,
    }
}

EXPECTED_TRACEABILITY = {
    "tests": [
        "verif/demo_tiny_alu#flags",
        "verif/demo_tiny_alu_cocotb#cocotb_flags",
    ],
    "blocks": ["demo_tiny_alu"],
    "docs": ["spec/demo_tiny_alu/README.md"],
    "goldens": ["spec/demo_tiny_alu/tiny_alu_model.py"],
}

EXPECTED_INTERFACE = {
    "ports": sorted(
        [
            "clk:input",
            "rst:input",
            "op:input",
            "a:input",
            "b:input",
            "y:output",
            "zf:output",
            "cf:output",
            "nf:output",
            "vf:output",
        ]
    ),
    "params": ["W"],
}


EXPECTED_DEEP_CHAIN = {
    "tests": [
        "verif/demo_tiny_alu#basic",
        "verif/demo_tiny_alu#ops_sweep",
        "verif/demo_tiny_alu_cocotb#cocotb_random",
    ],
    "testbenches": [
        "verif/demo_tiny_alu#tb_top",
        "verif/demo_tiny_alu_cocotb#tb_alu_random",
    ],
    "dut": ["demo_tiny_alu"],
    "dut_file": ["design/demo_tiny_alu/demo_tiny_alu.sv"],
    "docs": ["spec/demo_tiny_alu/README.md"],
    "goldens": ["spec/demo_tiny_alu/tiny_alu_model.py"],
}

EXPECTED_CHANGE_IMPACT = {
    "models": [
        "demo_tiny_alu_subsys_synth_top",
        "demo_tiny_alu_subsys_top",
        "ip_async_fifo",
        "ip_cdc_handshake",
        "ip_cdc_sync",
        "mem_subsys",
    ],
    "runs": [
        "synth/demo_tiny_alu_subsys#demo_tiny_alu_subsys_synth_generic",
        "synth/demo_tiny_alu_subsys#demo_tiny_alu_subsys_synth_nangate45",
        "verif/demo_tiny_alu_subsys#csr_smoke",
        "verif/demo_tiny_alu_subsys#fifo_stream",
        "verif/ip_async_fifo#smoke",
        "verif/ip_cdc_handshake#smoke",
        "verif/ip_cdc_sync#smoke",
    ],
}


TASKS = [
    Task(
        key="signal-trace",
        title="Trace a signal",
        question=f"In {TRACE_TOP}, what drives {TRACE_SIGNAL} and which instances load it?",
        expected=EXPECTED_TRACE,
        graph=trace_signal_graph,
        raw=trace_signal_raw,
    ),
    Task(
        key="tests-for-block",
        title="Tests for a block",
        question=f"Which tests exercise {BLOCK}, and at which reglvl?",
        expected=EXPECTED_TESTS,
        graph=tests_for_block_graph,
        raw=tests_for_block_raw,
    ),
    Task(
        key="traceability",
        title="Traceability chain",
        question=(
            f"For coverage item {COVITEM}: which tests claim it, which spec block "
            "declares it, which doc specifies it, which golden model implements it?"
        ),
        expected=EXPECTED_TRACEABILITY,
        graph=traceability_graph,
        raw=traceability_raw,
    ),
    Task(
        key="module-interface",
        title="Module interface",
        question=f"Summarize {IFACE_MODULE}'s interface: every port with its direction, plus the parameters.",
        expected=EXPECTED_INTERFACE,
        graph=interface_graph,
        raw=interface_raw,
    ),
    Task(
        key="deep-chain",
        title="Deep chain",
        question=(
            f"Coverage item {DEEP_ITEM} is short — which tests claim it, on which "
            "testbenches, against which DUT source file, and what spec doc and "
            "golden model is that DUT held to?"
        ),
        expected=EXPECTED_DEEP_CHAIN,
        graph=deep_chain_graph,
        raw=deep_chain_raw,
    ),
    Task(
        key="change-impact",
        title="Change impact on shared IP",
        question=(
            f"design/common/{IP_BLOCK}.sv changed — which models are affected, and "
            "which runs claimed by the root regression manifests must re-run?"
        ),
        expected=EXPECTED_CHANGE_IMPACT,
        graph=change_impact_graph,
        raw=change_impact_raw,
    ),
]


# ---------------------------------------------------------------------------
# driving it
# ---------------------------------------------------------------------------


def answer_floor(task: Task) -> int:
    """Return the token count of the expected answer as compact JSON, the minimum either route could cost."""
    return approx_tokens(json.dumps(task.expected, separators=(",", ":")))


def run_task(runner: Runner, task: Task) -> dict:
    result = {
        "key": task.key,
        "title": task.title,
        "question": task.question,
        "answer_tokens": answer_floor(task),
    }
    for name, fn in (("graph", task.graph), ("raw", task.raw)):
        route = Route(runner, name)
        try:
            route.run.answer = fn(route)
        except (RouteError, OSError, KeyError, json.JSONDecodeError) as exc:
            route.run.error = f"{type(exc).__name__}: {exc}"
        result[name] = {
            "tokens": route.run.tokens,
            "chars": route.run.chars,
            "calls": route.run.calls,
            "correct": route.run.answer == task.expected,
            "error": route.run.error,
            "answer": route.run.answer,
            "steps": [
                {"command": s.command, "tokens": s.tokens, "chars": len(s.output)}
                for s in route.run.steps
            ],
        }
    graph_tokens = result["graph"]["tokens"]
    result["ratio"] = (result["raw"]["tokens"] / graph_tokens) if graph_tokens else None
    return result


def project_provenance(project: Path) -> dict:
    def git(*args: str) -> str | None:
        proc = subprocess.run(
            ["git", "-C", str(project), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    graph_path = project / GRAPH_JSON
    graph_stats: dict = {}
    if graph_path.exists():
        data = json.loads(graph_path.read_text())
        graph_stats = {
            "nodes": len(data.get("nodes") or []),
            "links": len(data.get("links") or []),
            "bytes": graph_path.stat().st_size,
            "tiers": (data.get("graph") or {}).get("tiers"),
        }
        meta = graph_path.parent / "graph-meta.json"
        if meta.exists():
            graph_stats["fingerprint"] = json.loads(meta.read_text()).get("fingerprint")
    return {
        "project": str(project),
        "commit": git("rev-parse", "--short", "HEAD"),
        "describe": git("describe", "--tags", "--always", "--dirty"),
        "dirty": bool(git("status", "--porcelain")),
        "graph": graph_stats,
    }


def render_table(results: list[dict]) -> str:
    header = (
        "| Task | Answer | Raw tokens | Raw calls | Graph tokens | Graph calls | "
        "Ratio (raw / graph) | Both correct |"
    )
    sep = "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"
    rows = [header, sep]
    for res in results:
        ratio = res["ratio"]
        correct = res["graph"]["correct"] and res["raw"]["correct"]
        marks = []
        if not res["graph"]["correct"]:
            marks.append("graph WRONG")
        if not res["raw"]["correct"]:
            marks.append("raw WRONG")
        rows.append(
            f"| {res['title']} | {res['answer_tokens']} "
            f"| {res['raw']['tokens']} | {res['raw']['calls']} "
            f"| {res['graph']['tokens']} | {res['graph']['calls']} "
            f"| {ratio:.2f}x | {'yes' if correct else ', '.join(marks)} |"
        )
    raw_total = sum(r["raw"]["tokens"] for r in results)
    graph_total = sum(r["graph"]["tokens"] for r in results)
    rows.append(
        f"| **all {len(results)}** | {sum(r['answer_tokens'] for r in results)} "
        f"| **{raw_total}** | "
        f"{sum(r['raw']['calls'] for r in results)} | **{graph_total}** | "
        f"{sum(r['graph']['calls'] for r in results)} | "
        f"**{raw_total / graph_total:.2f}x** | |"
    )
    return "\n".join(rows)


def render_text(provenance: dict, results: list[dict]) -> str:
    out = [
        "graph vs raw token benchmark (rtl_buddy#381)",
        f"  project:     {provenance['project']}",
        f"  template at: {provenance['describe']}"
        + ("  (dirty)" if provenance["dirty"] else ""),
        f"  graph:       {provenance['graph'].get('nodes')} nodes, "
        f"{provenance['graph'].get('links')} links, "
        f"{provenance['graph'].get('bytes', 0) // 1024} KiB on disk",
        f"  proxy:       len(text) // {CHARS_PER_TOKEN}",
        "",
    ]
    for res in results:
        out.append(f"{res['title']} — {res['question']}")
        out.append(f"  answer   {res['answer_tokens']:>7} tokens  (the floor)")
        for route in ("raw", "graph"):
            data = res[route]
            state = "ok" if data["correct"] else f"WRONG ({data['error'] or 'answer'})"
            out.append(
                f"  {route:<6} {data['tokens']:>7} tokens  "
                f"{data['calls']:>2} calls  {state}"
            )
            for step in data["steps"]:
                out.append(f"           {step['tokens']:>6}  {step['command']}")
        ratio = res["ratio"]
        out.append(
            f"  ratio  {ratio:.2f}x (raw / graph; >1 means the graph is cheaper)"
        )
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-p",
        "--project",
        default=os.environ.get("RTL_BUDDY_TEMPLATE_ROOT"),
        help=(
            "project root to benchmark (default $RTL_BUDDY_TEMPLATE_ROOT); "
            "must already have artefacts/graph/graph.json"
        ),
    )
    parser.add_argument(
        "--rb",
        default=None,
        help="rtl-buddy entry point (default: this interpreter's -m rtl_buddy)",
    )
    parser.add_argument("--json", action="store_true", help="emit the full record")
    parser.add_argument(
        "--markdown", action="store_true", help="emit the docs results table"
    )
    args = parser.parse_args(argv)

    if not args.project:
        parser.error("--project is required (or set RTL_BUDDY_TEMPLATE_ROOT)")
    project = Path(args.project).resolve()
    if not (project / GRAPH_JSON).exists():
        parser.error(
            f"{project / GRAPH_JSON} not found — run `rb graph build` (all tiers) "
            "and `rb graph results` in the project first"
        )

    rb = args.rb.split() if args.rb else [sys.executable, "-m", "rtl_buddy"]
    runner = Runner(project=project, rb=rb)
    provenance = project_provenance(project)
    results = [run_task(runner, task) for task in TASKS]

    if args.json:
        print(json.dumps({"provenance": provenance, "tasks": results}, indent=2))
    elif args.markdown:
        print(render_table(results))
    else:
        print(render_text(provenance, results))

    wrong = [
        res
        for res in results
        if not (res["graph"]["correct"] and res["raw"]["correct"])
    ]
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
