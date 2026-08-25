---
name: rtl-buddy
description: Use rtl_buddy for SystemVerilog compile/sim, randomized tests, regressions, synthesis, implementation, lint, formal, filelists, and project YAML; route advanced workflows to specialist skills or bundled docs.
---

# rtl_buddy

Run `rb --version` at the top of every run summary.

Use this overview for basic work. For syntax or schemas, start with `rb --help`,
`rb <command> --help`, `rb docs list`, `rb docs show agents`, and
`rb --machine docs show reference/yaml`. Check `rb docs show known-issues` before
changing configuration to work around surprising behavior.

## Always use `--machine`

All agent invocations must use `--machine` so `rtl_buddy.log` is JSONL.
Structured result commands print one JSON envelope to stdout; `rb docs show` is
the exception and returns the requested page as bare JSON. Parse
`payload.results[*].result` and `desc`; never scrape the human table. For `test`,
`randtest`, and `regression`, exit 0 means no real FAIL (including `NA`/`XFAIL`),
1 means a real FAIL or strict `XPASS`, and 2 means fatal configuration/environment
failure. Other commands define their own exit contract; the payload has details.

## Basic tests and regressions

```bash
rb --machine test smoke -c path/to/tests.yaml
rb --machine test -c path/to/tests.yaml
rb --machine regression -c path/to/regression.yaml
```

`test` runs one suite; `regression` walks the suites in its manifest. Use
`rb test --help` for current selectors and `rb regression --help` for levels.

UVM uses report thresholds and cocotb uses `cocotb_results.xml`. Other sims must
emit a line beginning `PASS` or `FAIL` in `artefacts/<test>/test.log`; add an
`ERR:` or `FAT:` line after `FAIL` so the result explains itself.

## YAML and execution context

- `root_config.yaml` configures project-wide builders, tools, and flow defaults.
- `tests.yaml` / `models.yaml` define suite tests/testbenches and design filelists.
- `regression.yaml` lists suites; flow YAML files define named runs.
- Outputs anchor on the config file's directory, not necessarily the shell cwd.
  Regression suite outputs anchor on each `tests.yaml`; its orchestration output
  anchors on `regression.yaml`. Explicit CLI output paths follow shell semantics.

## Route specialized work

- `rtl-buddy-test`: randtest, result parsing, timeouts, artefacts, shared builds.
- `rtl-buddy-dispatch`: Slurm/local parallelism, resources, retries, OOMs.
- `rtl-buddy-graph`: graph, hierarchy, source lookup, hub, and MCP queries.
- `rtl-buddy-fpv`: formal proofs, UNKNOWN, vacuity/COI, mutation guardrails.
- `rtl-buddy-implementation`: synthesis, P&R, power, FPGA, and XPLR loops.
- Docs: CDC, lint, coverage, wave, mutation, config fields, and command how-tos.
