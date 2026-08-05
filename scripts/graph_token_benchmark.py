#!/usr/bin/env python3
"""Token-efficiency benchmark: graph queries vs raw-file context (#381).

Graphify's ~70x tokens-per-query claim is measured on software corpora.
This script measures *our* number, on an RTL project, before SKILL.md
steers every agent at the graph.

Four questions an agent actually asks are answered twice:

* the **graph route** — `rb --machine graph query|path|explain` against
  `artefacts/graph/graph.json`, the surface #380 built;
* the **raw route** — filelist / grep / whole-file reads, which is what
  an agent does in a tree with no graph.

Both routes end in a machine-comparable answer, and both are checked
against a hand-written key (`EXPECTED`, verified against the template
sources by hand — see the per-task comments). A route that gets the
answer wrong does not get to be the cheap one.

Token proxy
-----------
`len(text) // 4`, applied to **every byte that crosses into the agent's
context**: the command the agent types plus the bytes it reads back. No
tokenizer is imported on purpose — a real BPE would tie the published
number to one vendor's vocabulary and add a dependency, and the ratio
between two routes is insensitive to the constant. Four characters per
token is the usual English/JSON rule of thumb; identifier-dense JSON
runs a little denser than that, and both routes pay the same bias.

What is *not* counted: `rb graph build` itself. The graph is an index —
it is built once per source change and amortized over every question,
exactly like a `ctags` database. Its cost is reported separately in the
header, not charged per query.

Usage
-----
    uv run python scripts/graph_token_benchmark.py --project /path/to/rtl-buddy-project-template
    uv run python scripts/graph_token_benchmark.py -p ... --markdown   # docs table
    uv run python scripts/graph_token_benchmark.py -p ... --json       # machine

The project must have been through `rb graph build` (all tiers) and
`rb graph results` first; the script refuses to guess.
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
    """The documented proxy: four characters to a token, floor."""
    return len(text) // CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# route plumbing
# ---------------------------------------------------------------------------


@dataclass
class Step:
    """One thing the agent did, and everything it had to read back."""

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
    """Records every command a route runs and what it read back.

    The rule both routes are held to: an answer may only be derived from
    text this object handed back. Neither route is allowed to peek at
    the project any other way — otherwise the token count stops being
    the cost of the answer.
    """

    def __init__(self, runner: Runner, name: str) -> None:
        self.runner = runner
        self.run = RouteRun(route=name)
        self._read: dict[str, str] = {}

    # -- graph route -------------------------------------------------
    def machine(self, *args: str) -> dict:
        """`rb --machine <args>` -> the payload, with the cost recorded."""
        argv = [*self.runner.rb, "--machine", *args]
        printed = "rb --machine " + " ".join(_quote(a) for a in args)
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
        return envelope.get("payload") or {}

    # -- raw route ---------------------------------------------------
    def shell(self, argv: list[str], *, allow_fail: bool = True) -> str:
        """A shell command an agent would run, and its output."""
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
        """Read a whole file, the way an agent's file-read tool does.

        A file already in the transcript is free the second time: an
        agent does not re-read what it is still looking at, and charging
        it twice would flatter the graph route.
        """
        if rel in self._read:
            return self._read[rel]
        text = (self.runner.project / rel).read_text()
        self._read[rel] = text
        self.run.steps.append(Step(f"cat {rel}", text))
        return text

    def read_lines(self, rel: str, start: int, end: int) -> str:
        """Read a cited span — the "locate in the graph, cite from source" half."""
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
# task 1 — trace a signal: driver + loads across the hierarchy
# ---------------------------------------------------------------------------
#
# "In demo_cdc_open_top, what drives rst_b_n and which instances load it?"
#
# Hand-checked against design/demo_cdc_open/demo_cdc_open_top.sv (and the
# four child modules for the port directions):
#   line 52-54  u_reset_sync_b (cdc_open_reset_sync) .rst_n  -> output -> DRIVER
#   line 66-68  u_flag_sync    (cdc_open_sync)       .rst_n  -> input  -> load
#   line 71-79  u_gray_bus     (cdc_open_gray_bus)   .dst_rst_n -> input -> load
#   line 82-92  u_handshake    (cdc_open_handshake)  .dst_rst_n -> input -> load

TRACE_TOP = "demo_cdc_open_top"
TRACE_SIGNAL = "rst_b_n"
TOP_INSTANCE = f"inst:{TRACE_TOP}/{TRACE_TOP}"


def trace_signal_graph(route: Route) -> dict:
    """One explain for the parent, one per child, one per port hit.

    There is no node for an internal net — `rst_b_n` exists in the graph
    only as the `actual` on a `connects` edge — so the walk is: the top
    instance's children, each child's `connects` edges, and then the
    ports those edges land on, because a port's `dir` is what separates
    the driver from the loads and only `explain` reports it.
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
    """grep for the net, read the parent, read every child module it binds."""
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
# task 2 — which tests exercise block X, at which reglvl?
# ---------------------------------------------------------------------------
#
# Hand-checked against verif/*/tests.yaml (every `model: "demo_tiny_alu"`):
#   verif/demo_tiny_alu        basic, ops_sweep, flags, random   reglvl 0
#   verif/demo_tiny_alu_cocotb cocotb_random, cocotb_flags       reglvl 1000
#   verif/demo_tiny_alu_sc     basic_sc                          reglvl 0
# demo_tiny_alu_subsys runs model demo_tiny_alu_subsys_top and is *not*
# an answer — the name-substring trap this task exists to catch.

BLOCK = "demo_tiny_alu"
MODEL_NODE = f"model:design/{BLOCK}/models.yaml#{BLOCK}"


def tests_for_block_graph(route: Route) -> dict:
    """model -> testbenches (`exercises`) -> tests (`runs_on`) -> reglvl.

    Started from the model node rather than a keyword search on purpose:
    `--type test demo_tiny_alu` would also score the *subsys* tests,
    and a name is not evidence. `reglvl` is a node attribute, so the
    last hop costs one `explain` per test.
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
    """grep the verif tree for the model name, read every suite that hits."""
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
# task 3 — test -> coverage item -> spec doc -> golden model
# ---------------------------------------------------------------------------
#
# Hand-checked: SAND-FUNC-FLAG-C-ADD is declared by block demo_tiny_alu
# (spec/demo_tiny_alu/specs.yaml), documented by spec/demo_tiny_alu/README.md,
# claimed by verif/demo_tiny_alu#flags and verif/demo_tiny_alu_cocotb#cocotb_flags,
# and the block's golden model is spec/demo_tiny_alu/tiny_alu_model.py.

COVITEM = "SAND-FUNC-FLAG-C-ADD"


def traceability_graph(route: Route) -> dict:
    """`query` the item, then `explain` the block that declares it.

    Depth 1, not 2: at depth 2 the block's other fifteen coverage items
    are nearer than the doc and the golden model and the 25-neighbour
    budget truncates before reaching them — a bigger answer that is
    also an incomplete one. Two calls that terminate is the honest
    route.
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
    """grep the id, read the suites that claim it and the spec that declares it."""
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
# task 4 — summarize a module's interface for reuse
# ---------------------------------------------------------------------------
#
# Hand-checked against design/demo_tiny_alu/demo_tiny_alu.sv lines 18-30:
# 10 ports (clk, rst, op, a, b in; y, zf, cf, nf, vf out) and one
# parameter, W.

IFACE_MODULE = "demo_tiny_alu"


def interface_graph(route: Route) -> dict:
    """Locate the ports in the graph, then cite the one span that declares them.

    `--depth 0` because the neighbourhood of a port is its instance
    connections, which this question does not ask about. A port's `dir`
    and width are not in a match summary, so the route takes the
    documented "locate in the graph, cite from source" path: the port
    nodes hand over the file and the exact line range to read.

    `--limit 20` for ten ports is not slack: no edge ties a port to its
    module, so this is a substring search, and every
    `port:demo_tiny_alu_subsys_*.…` scores exactly the same 12. The ten
    real ones sort first (``.`` sorts before ``_``), so a limit of
    twice the expected answer is what proves the list is complete —
    the run stops seeing the prefix before the limit does.
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

    files = {m["file"] for m in port_nodes}
    lines = [m["line"] for m in port_nodes if m.get("line")]
    file = sorted(files)[0]
    text = route.read_lines(file, min(lines), max(lines))
    declared = _port_decls(text)
    return {
        "ports": sorted(f"{name}:{direction}" for name, direction, _t in declared),
        "params": param_names,
    }


def interface_raw(route: Route) -> dict:
    """grep for the module declaration, then read the file it lives in."""
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
# the small amount of SystemVerilog / YAML parsing the raw route needs
# ---------------------------------------------------------------------------
#
# This is what an agent does in its head after reading a file. It is
# deliberately regex-thin: the benchmark charges the raw route for the
# *bytes it had to read*, and how they are then interpreted costs no
# tokens either way.

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
    """module name -> [(port, direction, declared type)]."""
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


def _instance_bindings(text: str, signal: str) -> list[tuple[str, str, str]]:
    """[(module, instance, formal)] for every port bound to `signal`."""
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
    """The block of a tests.yaml under a top-level `tests:` / `testbenches:`."""
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


def _tests_with_covers(text: str) -> list[tuple[str, str | None, object, list[str]]]:
    """[(test name, model, reglvl, covers)] out of a tests.yaml."""
    if not text:
        return []
    section = _yaml_section(text, "tests")
    entries: list[tuple[str, str | None, object, list[str]]] = []
    starts = [m.start() for m in _TEST_ENTRY.finditer(section)]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(section)
        chunk = section[start:end]
        name = _TEST_ENTRY.match(chunk).group("name")
        model = re.search(r"^\s*model:\s*\"?([\w.\-]+)\"?", chunk, re.MULTILINE)
        reglvl = re.search(r"^\s*reglvl:\s*(\S+)", chunk, re.MULTILINE)
        covers = re.findall(r"^\s*-\s*\"?([A-Z][\w\-]+)\"?\s*$", chunk, re.MULTILINE)
        value: object = None
        if reglvl:
            raw = reglvl.group(1).split("#")[0].strip()
            value = int(raw) if raw.lstrip("-").isdigit() else raw
        entries.append((name, model.group(1) if model else None, value, covers))
    return entries


def _tests_in_yaml(text: str) -> list[tuple[str, str | None, object]]:
    return [
        (name, model, reglvl) for name, model, reglvl, _c in _tests_with_covers(text)
    ]


def _spec_blocks(text: str) -> list[tuple[str, list[str], list[str]]]:
    """[(block name, docs, coverage-item ids)] out of a specs.yaml."""
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
# the task set
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
]


# ---------------------------------------------------------------------------
# driving it
# ---------------------------------------------------------------------------


def answer_floor(task: Task) -> int:
    """The answer itself, as compact JSON — the floor either route could hit.

    Reported next to both routes because it is the number that says
    where a route's cost went: a route that spends 10 000 tokens
    delivering a 60-token answer is not being taxed by the corpus, it is
    being taxed by its own payload shape.
    """
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
        f"| **all four** | {sum(r['answer_tokens'] for r in results)} "
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
