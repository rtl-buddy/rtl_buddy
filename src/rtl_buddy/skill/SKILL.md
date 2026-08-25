---
name: rtl-buddy
description: Use rtl_buddy for basic RTL testing, analysis, and implementation workflows, with routing to focused skills and bundled docs.
---

# rtl_buddy

Run `rb --version` at the top of every run summary.

Use this skill for basic work and feature discovery. Start with `rb --help`,
`rb <command> --help`, `rb --machine docs list`, or
`rb --machine docs show quickstart`.
Use `rb --machine docs show reference/yaml` for schemas and
`rb --machine docs show known-issues` before working around surprising behavior.
All documentation slugs below mean `rb --machine docs show <slug>`.
Before running a named flow, discover the real config and entry names with
`--list` where supported. Names and paths below show command shape; do not copy
placeholders into a project without checking them.

## Use `--machine` for automation

Commands with structured results return one JSON envelope; `rb docs show` is the
exception and returns the requested page as bare JSON. For row-producing tests
and flows, parse `payload.results[*].result` and `desc`; inspect the
command-specific payload otherwise. Never scrape the human table.

`filelist`, `hier`, `wave`, and `axi-profile` are output/pass-through commands
without an rtl_buddy envelope. `rb mcp` owns stdout as a long-running JSON-RPC
server. Machine mode makes `rtl_buddy.log` JSONL for commands that initialize a
log; it does not make every command create one.

Run and regression commands normally exit 0 when every result counts as
successful, 1 for any `FAIL` or strict `XPASS`, and 2 for fatal configuration or
environment errors. Simulation exits 0 with no real failure, including `NA`/`XFAIL`.
Reporting, audit, and pass-through commands define their own codes; use the
relevant specialist or bundled docs page.

## Tests, random tests, and regressions

Use `test` for a named simulation or all tests in one suite, `randtest` to repeat
one test across seeds, and `regression` to run suites from a manifest.

```bash
rb --machine test smoke -c path/to/tests.yaml
rb --machine randtest smoke 20 -c path/to/tests.yaml
rb --machine regression -c path/to/regression.yaml
```

UVM uses report thresholds and cocotb uses `cocotb_results.xml`. Other sims must
emit a line beginning `PASS` or `FAIL` in `artefacts/<test>/test.log`; add an
`ERR:` or `FAT:` line after `FAIL` so the result explains itself. Use the
`rtl-buddy-test` skill for selectors, timeouts, artefacts, verdict triage, and
shared-build behavior. Docs: `concepts/tests` and `concepts/regressions`.

## Project configuration and filelists

`root_config.yaml` selects project-wide builders, tools, and flow defaults.
`tests.yaml` and `models.yaml` define suites, testbenches, and design sources;
flow YAML files define named runs, while `*_regression.yaml` files group them.
Generate a tool filelist from a model when another command needs the same source
closure:

```bash
rb --machine filelist my_model run.f -c path/to/models.yaml
```

Config-relative inputs and default outputs anchor on the config file's directory,
not necessarily the shell cwd. Regression suite outputs anchor on each suite
config; orchestration output anchors on the regression manifest. Explicit CLI
output paths follow shell semantics. Docs: `concepts/execution-context`,
`concepts/root-config`, and `reference/yaml`.

## Lint and CDC

Use `lint` for Verible style/static checks and `cdc` for structural clock-domain
crossing analysis. Run them before expensive simulation or implementation; use
their regression commands for project-wide gates.

```bash
rb --machine lint -c path/to/lint.yaml
rb --machine cdc -c path/to/cdc.yaml
```

Use `--list` before choosing a named check. CDC can also emit or audit timing
constraints; read the command help instead of guessing the constraint mode. Use
the `rb verible` group when direct Verible lint/format operations are needed.
Docs: `reference/cli` and `reference/yaml`.

## Formal verification and mutation testing

Use `fpv` to prove or cover assertions with SymbiYosys, and `fpv-regression` to
run a formal suite. Use mutation testing after the harness works to measure
whether deliberate RTL changes are detected.

```bash
rb --machine fpv smoke -c path/to/fpv.yaml
rb --machine fpv-regression -c path/to/fpv_regression.yaml
rb --machine mut list -c path/to/mut.yaml
```

Use the `rtl-buddy-fpv` skill for UNKNOWN, vacuity, cone-of-influence, frontend,
and mutation guardrails. Docs: `concepts/fpv` and `concepts/mut`.

## Synthesis, place-and-route, power, and FPGA

Use `synth` to turn RTL into a netlist and, where supported, area/timing metrics;
`pnr` handles physical implementation, `power` handles activity-based analysis,
and `fpga` runs a vendor or open-source FPGA flow. Run a named entry first; use
the corresponding regression command where available after understanding it.
`saif` converts simulation activity for power flows that need that interchange.

```bash
rb --machine synth --list
rb --machine pnr --list
rb --machine power --list
rb --machine fpga --list
```

A completed tool run is not the same as meeting timing, area, power, or routing
targets. Use the `rtl-buddy-implementation` skill for result interpretation,
timing closure, and XPLR loops. Docs: `concepts/synthesis`, `concepts/pnr`,
`concepts/power`, and `concepts/fpga`.

## Coverage, waveforms, and AXI profiling

Use `cov` to inspect existing coverage artefacts. `wave` opens an existing test
waveform or may run a debug simulation to create one; `wave-fpv` opens a failed
proof's counterexample. `axi-profile` discovers buses, generates a monitor, or
turns a simulation trace into performance data.

```bash
rb --machine cov summary
rb --machine wave smoke -c path/to/tests.yaml
rb --machine axi-profile run smoke -c path/to/tests.yaml
```

`cov summary` and `axi-profile run` consume existing simulation artefacts.
`wave` runs or reruns the named test in debug mode when needed;
`axi-profile discover` and `gen-monitor` are setup steps before simulation.
Docs: `concepts/coverage`, `concepts/wave`, and `concepts/axi-profile`.

## Design graph, hierarchy, hub, and MCP

Use `hier` to render a model or testbench tree and `hier-query` for exact module,
instance, connection, or source lookups. Build the graph when a question crosses
RTL, tests, models, specs, results, or source locations; query it instead of
manually joining those relationships.

```bash
rb --machine hier my_model
rb --machine graph build
rb --machine graph query "which tests cover ITEM"
```

Use the `rtl-buddy-graph` skill for choosing direct reads versus graph queries,
source citations, result overlays, and graph refreshes. `rb mcp` exposes the same
query surface over stdio; `rb hub` coordinates the browser view, editor, coverage,
and waveform tools. Docs: `concepts/graph`, `concepts/hier`, and `concepts/hub`.

## Spec traceability and design-space exploration

Use `spec` to find requirements missing a design link or verification coverage.
Use `xplr` as an agent-facing ledger for repeatable implementation experiments,
comparisons, and Pareto-frontier tracking.

```bash
rb --machine spec check-design
rb --machine spec check-coverage
rb --machine xplr list
rb --machine xplr frontier
```

XPLR records experiments; it does not choose the next experiment. Use the
`rtl-buddy-implementation` skill before running an optimization loop. Docs:
`concepts/spec-traceability` and `concepts/xplr`.

## Dispatch and tool readiness

Use `tool-check` before a flow that shells out to external tools. Use local
parallel dispatch for independent local workers and Slurm dispatch for queued,
resource-governed regression work.

```bash
rb --machine tool-check --required-for regression
rb --machine regression --dispatch local-parallel -j 8
rb --machine regression --dispatch slurm
```

Tool readiness is manifest-level; also inspect the selected run and root config
because project-specific tool paths and backends are not reconciled by the check.
Use the `rtl-buddy-dispatch` skill for resource sizing, shared-build dependencies,
OOMs, scheduler timeouts, retries, and missing job envelopes. Docs:
`concepts/tool-check` and `concepts/dispatch`.
