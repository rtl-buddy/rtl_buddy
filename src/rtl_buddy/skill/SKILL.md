---
name: rtl-buddy
description: Use rtl_buddy for basic RTL testing, analysis, and implementation workflows, with routing to focused skills and bundled docs.
---

# rtl_buddy

Run `rb --version` at the top of every run summary.

Start with command help or `rb --machine docs list`. The local `reference/yaml` and `known-issues` docs cover schemas and surprises. Find real config and entry names with `--list`; the paths below only show command shape.

## Use `--machine` for automation

Structured commands print one JSON envelope; `rb docs show` is the exception and prints the requested page as bare JSON. For row-producing tests and flows, read `payload.results[*].result` and `desc`; for other commands, read the command-specific payload. Never scrape the human table.

`filelist`, `hier`, `wave`, and `axi-profile` are pass-through commands. `rb mcp` owns stdout. Machine mode makes initialized `rtl_buddy.log` files JSONL.

Run and regression commands exit 0 when every result counts as successful, 1 for a `FAIL`, unknown `NA` or strict `XPASS`, and 2 for a fatal config or environment error. A sim exits 0 with no real failure, including an intentional early-stop `NA` or `XFAIL`. Reporting, audit, and pass-through commands have their own codes; see the specialist skill or docs page.

## Tests, random tests, and regressions

`test` runs a named test or every test in a suite, `randtest` repeats one test across seeds, and `regression` runs the suites in a manifest.

```bash
rb --machine test smoke -c path/to/tests.yaml
rb --machine randtest smoke 20 -c path/to/tests.yaml
rb --machine regression -c path/to/regression.yaml
```

UVM tests use report thresholds and cocotb uses `cocotb_results.xml`. Other sims must print a line starting `PASS` or `FAIL` to `artefacts/<test>/test.log`; put an `ERR:` or `FAT:` line after `FAIL` to explain it. Use the `rtl-buddy-test` skill for selectors, timeouts, artefacts, verdict triage, and shared builds. Docs: `concepts/tests`, `concepts/regressions`.

## Project configuration and filelists

`root_config.yaml` sets project-wide builders, tools, and flow defaults. `tests.yaml` and `models.yaml` define suites, testbenches, and design sources. Flow YAML files define named runs and `*_regression.yaml` files group them. Generate a filelist from a model when another tool needs the same sources:

```bash
rb --machine filelist my_model run.f -c path/to/models.yaml
```

`elab` parses, type-checks, and elaborates the same model. A bare run needs no profile, optional `models.yaml` profiles add per-gate changes, and a manifest groups profiles for regression.

```bash
rb --machine elab my_model -c path/to/models.yaml
rb --machine elab-regression -c path/to/elab_regression.yaml
```

Config-relative inputs and default outputs anchor on the config file's directory, which may not be the shell cwd. Regression suite outputs anchor on each suite config and orchestration output on the manifest. Explicit CLI output paths follow shell semantics. Docs: `concepts/execution-context`, `concepts/root-config`, `concepts/elaboration`, `reference/yaml`.

## Lint and CDC

`lint` runs Verible style and static checks. `cdc` runs structural clock-domain-crossing analysis and can emit or audit timing constraints; read its help for the constraint modes. Run both before expensive simulation or implementation, and use their regression commands for project-wide gates. Use `rb verible` for direct Verible lint or format operations.

```bash
rb --machine lint -c path/to/lint.yaml
rb --machine cdc -c path/to/cdc.yaml
```

Run `--list` to see check names. Docs: `reference/cli`, `reference/yaml`.

## Formal verification and mutation testing

`fpv` proves or covers assertions with SymbiYosys and `fpv-regression` runs a formal suite. Run mutation testing after the harness works, to measure whether deliberate RTL changes are caught.

```bash
rb --machine fpv smoke -c path/to/fpv.yaml
rb --machine fpv-regression -c path/to/fpv_regression.yaml
rb --machine mut list -c path/to/mut.yaml
```

Use the `rtl-buddy-fpv` skill for UNKNOWN, vacuity, cone-of-influence, frontend, and mutation guardrails. Docs: `concepts/fpv`, `concepts/mut`.

## Synthesis, place-and-route, power, and FPGA

`synth` turns RTL into a netlist with area and timing metrics where supported, `pnr` does physical implementation, `power` does activity-based analysis, and `fpga` runs a vendor or open-source FPGA flow. Run one named entry first, then the regression command once you understand it. `saif` converts simulation activity for power flows.

```bash
rb --machine synth --list
rb --machine pnr --list
rb --machine power --list
rb --machine fpga --list
rb --machine phys summary
```

A completed tool run does not mean timing, area, power, or routing targets were met. Use the `rtl-buddy-implementation` skill for result interpretation, timing closure, and XPLR loops. `phys` reads the per-module and per-instance model that a finished `synth` or `power` run wrote and starts no tool. Docs: `concepts/synthesis`, `concepts/pnr`, `concepts/power`, `concepts/fpga`, `concepts/phys`.

## Coverage, waveforms, and AXI profiling

`cov` inspects existing coverage artefacts. `wave` opens an existing test waveform, running or rerunning the named test in debug mode when needed; `wave-fpv` opens a failed proof's counterexample. `axi-profile` finds buses, generates a monitor, or turns a simulation trace into performance data. `discover` and `gen-monitor` are setup steps before simulation; `run` consumes existing simulation artefacts, as does `cov summary`.

```bash
rb --machine cov summary
rb --machine wave smoke -c path/to/tests.yaml
rb --machine axi-profile run smoke -c path/to/tests.yaml
```

Docs: `concepts/coverage`, `concepts/wave`, `concepts/axi-profile`.

## Design graph, hierarchy, hub, and MCP

`hier` renders a model or testbench tree and `hier-query` answers exact module, instance, connection, or source lookups. Build the graph and query it when a question spans RTL, tests, models, specs, results, or source locations, instead of joining them by hand.

```bash
rb --machine hier my_model
rb --machine graph build
rb --machine graph query "which tests cover ITEM"
```

Use the `rtl-buddy-graph` skill to choose between direct reads and graph queries, and for source citations, result overlays, and refreshes. `rb mcp` serves the same queries over stdio; `rb hub` coordinates the browser view, editor, coverage, and waveform tools. Docs: `concepts/graph`, `concepts/hier`, `concepts/hub`.

## Spec traceability and design-space exploration

`spec` finds requirements with no design link or verification coverage. `xplr` is an agent-facing ledger for repeatable implementation experiments, comparisons, and Pareto-frontier tracking. It records experiments and does not choose the next one.

```bash
rb --machine spec check-design
rb --machine spec check-coverage
rb --machine xplr list
rb --machine xplr frontier
```

Use the `rtl-buddy-implementation` skill before an optimization loop. Docs: `concepts/spec-traceability`, `concepts/xplr`.

## Dispatch and tool readiness

Run `tool-check` before a flow that calls external tools. Use local-parallel dispatch for independent workers on one host and Slurm dispatch for queued, resource-governed regressions.

```bash
rb --machine tool-check --required-for regression
rb --machine regression --dispatch local-parallel -j 8
rb --machine regression --dispatch slurm
```

`tool-check` covers the tool manifest only. It does not reconcile project-specific tool paths and backends, so also inspect the selected run and root config. Use the `rtl-buddy-dispatch` skill for resource sizing, shared-build dependencies, OOMs, scheduler timeouts, retries, and missing job envelopes. Docs: `concepts/tool-check`, `concepts/dispatch`.
