---
description: Configure and run mutation campaigns with FPV or simulation kill oracles, scoped files, budgets, and machine-readable reports.
---

# Mutation testing

`rb mut` injects small SystemVerilog changes (mutants) and measures whether your configured verification catches them. Use it to find holes in a testbench or property set. It needs `rtl-buddy-xeno[verible,slang] >= 0.1.0`; without a compatible engine it exits with an installation hint.

```bash
uv add "rtl_buddy[mut]"
```

## Configure a campaign

One `mut.yaml` defines one campaign:

```yaml
rtl-buddy-filetype: mut_config

model: demo_top
model_path: ../../design/demo_top/models.yaml
design_file: ../../design/demo_top/rtl/alu.sv

operators: [arith_flip, bit_op_flip, cond_negate, cond_const]

verify:
  fpv_config: ../../fpv/demo/fpv.yaml
  verification: demo_fpv_alu_safety
  test_config: ../../verif/demo/tests.yaml
  tests: [alu_smoke, alu_random]
  assertions: true

budget:
  max_mutants: 100
  per_file_cap: null
  time_budget_minutes: null
  schedule: sequential

scope:
  include: []
  exclude: []
```

`design_file` must be inside the directory containing `models.yaml`, because each mutant is evaluated in an isolated copy of that tree. It is also the baseline-oracle target. See [YAML formats](../reference/yaml.md) for the full schema.

## Choose kill oracles

At least one oracle is required. When both are configured, either can kill a mutant.

| Oracle | Required fields | A mutant is killed when |
|---|---|---|
| FPV | `fpv_config`, `verification` | The named baseline PASS becomes FAIL |
| Simulation | `test_config`; optional `tests`, `assertions` | A selected test fails or an SVA assertion fires |

Simulation enables Verilator assertions by default; see [Assertion-based verification](abv-simulation.md).

## Choose operators

| Operator | Mutation |
|---|---|
| `arith_flip` | Flip an arithmetic operator |
| `bit_op_flip` | Flip a bitwise or logical operator |
| `cond_negate` | Negate a condition |
| `cond_const` | Replace a condition with a constant |
| `assign_drop` | Drop an assignment |
| `port_binding_swap` | Swap two port bindings |

An empty operator list, or an operator the installed engine does not support, is fatal.

## Scope a hierarchical campaign

An empty `scope` mutates only `design_file` and does not need `rtl-buddy-view`. A non-empty `include` or `exclude` resolves files through the hierarchy graph, so `rtl-buddy-view` must be on `PATH`.

- Patterns are case-sensitive `fnmatch` shell globs. `**` is not recursive; spell out path segments.
- Patterns match both instance paths and source paths, including model-relative and absolute source paths.
- An empty `include` selects all hierarchy files; matching `exclude` entries remove files. A scope that selects no files is fatal.
- Mutation is file-based: a module instantiated several times is mutated once, in its source file.
- `schedule: sequential` processes scoped files in sorted order; `round_robin` interleaves them.
- `per_file_cap` limits each file and `max_mutants` limits the campaign. `design_file` stays the baseline target.

## Run and score

```bash
rb mut list
rb mut list -c mut/demo/mut.yaml
rb mut run -c mut/demo/mut.yaml
rb mut score mut/demo/artefacts/mut/demo_top/mut_report.json
```

- `list` shows candidate sites without mutating.
- `run` uses the `debug` builder mode by default, evaluates the baseline and the mutants, then writes the report.
- `score` recomputes the score from an existing report without rerunning verification.

Paths and artefacts anchor to the selected `mut.yaml`, not the shell directory; see [Execution Context](execution-context.md).

## Interpret results

Each mutant has one outcome:

- `KILLED`: an oracle caught the change.
- `SURVIVED`: every oracle passed. Inspect these verification gaps first.
- `ERRORED`: the mutant did not elaborate or compile. It is excluded from scoring.

```text
mutation score = killed / (killed + survived)
```

The score is `n/a` when nothing is scorable. Surviving mutants whose operator predicted observable signal changes are also reported as predicted-observable misses.

The report is `<mut.yaml dir>/artefacts/mut/<campaign>/mut_report.json`, with the baseline verdict, totals, score, and each mutant's operator, outcome, verdict, diff summary and predicted signals. Under `--machine`, `mut list` returns `sites`; `mut run` and `mut score` return `report`.

## Exit codes

- `mut run` exits 0 when any result is scorable and 1 when the score is `n/a`. It does not gate on a score threshold.
- `mut list` and `mut score` exit 0 on success.
- Configuration, engine and report errors are fatal.
