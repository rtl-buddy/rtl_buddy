---
description: How to score a verification suite with rtl_buddy mutation testing via the rb mut command, mut.yaml, the rtl-buddy-xeno engine, and FPV or simulation kill oracles.
---

# Mutation Testing

> **Integration type:** Pluggable engine. `rb mut` orchestrates the external [`rtl-buddy-xeno`](https://github.com/rtl-buddy/rtl-buddy-xeno) mutation engine; rtl_buddy supplies the config, the kill oracles, and the scoring/report.
>
> **Optional dependency:** the engine is **not** installed by default. Install it with `pip install "rtl-buddy-xeno[verible,slang]"` (the `verible`/`slang` extras provide the Verible CST + pyslang toolchain the operators need). Running `rb mut` without it raises a fatal error with this hint.
>
> See also: [Formal Property Verification](fpv.md), [Assertion-Based Verification (sim)](abv-simulation.md).

`rb mut` measures how good your verification actually is. It mutates a single SystemVerilog design file — introducing small, deliberate bugs (mutants) — and checks whether your verification *kills* each mutant (catches the injected bug). The **mutation score** is the fraction of viable mutants that were killed; a high score means your properties and tests genuinely exercise the design, while survivors point at gaps.

Unlike `fpv.yaml` (a list of verifications), one `mut.yaml` describes a **single mutation campaign** — one design file under test — so `rb mut list` / `rb mut run` are unambiguous about what is being mutated.

## How scoring works

Each generated mutant is classified into one of three outcomes:

- **KILLED** — the verification caught the mutant (good).
- **SURVIVED** — the mutant slipped through every oracle (a coverage gap).
- **ERRORED** — the mutant broke elaboration/compilation, so it could not be scored.

```
mutation score = killed / (killed + survived)
```

`ERRORED` mutants are dropped from the denominator, so a mutant that simply fails to elaborate never inflates or deflates the score. When nothing was scorable (every mutant errored, or none were generated) the score is reported as `n/a`.

A **SURVIVED** mutant whose operator *predicted* it would perturb observable signals is the highest-signal finding — it means a change the engine expected to be observable still passed your checks. These are flagged separately as **predicted-observable misses (weak properties)**.

## Kill oracles

A mutant is *killed* when a configured oracle flags it. You configure at least one of two oracles in the `verify:` block (you may configure both — a mutant is killed if **either** oracle catches it):

| Oracle | Configured by | A mutant is killed when |
|---|---|---|
| **FPV** | `fpv_config` + `verification` | the named verification's proof flips from the unmutated baseline `PASS` to `FAIL` |
| **Simulation** | `test_config` (+ optional `tests`, `assertions`) | a test in the suite `FAIL`s or an SVA assertion fires |

The simulation oracle compiles SVA in via Verilator `--assert` by default (`assertions: true`); it is much weaker without assertions, so leave them on unless you have a reason not to. See [Assertion-Based Verification (sim)](abv-simulation.md) for how firings are detected.

## Mutation config: `mut.yaml`

```yaml
rtl-buddy-filetype: mut_config

model: demo_top
model_path: "../../design/demo_top/models.yaml"
design_file: "../../design/demo_top/rtl/alu.sv"

operators:
  - arith_flip
  - bit_op_flip
  - cond_negate
  - cond_const

verify:
  # FPV oracle (fpv_config requires verification)
  fpv_config: "../../fpv/demo/fpv.yaml"
  verification: "demo_fpv_alu_safety"
  # Simulation oracle (optional; combine with or use instead of FPV)
  test_config: "../../verif/demo/tests.yaml"
  tests: ["alu_smoke", "alu_random"]   # empty/omitted = every test in the suite
  assertions: true

budget:
  max_mutants: 100
  per_file_cap: null
  time_budget_minutes: null
  schedule: "sequential"

scope:                                 # optional; empty = single-file default
  include: []
  exclude: []
```

### Fields

| Field | Description |
|---|---|
| `model` | Model name within the referenced `models.yaml` |
| `model_path` | Path to the `models.yaml`, resolved relative to `mut.yaml` |
| `design_file` | The single SystemVerilog file to mutate, resolved relative to `mut.yaml`. **Must live within the model directory** (the directory containing `models.yaml`) so per-mutant isolation can copy the tree and splice the mutant in |
| `operators` | Non-empty list of mutation operators (see below). An empty list or an unknown operator is a fatal config error |
| `verify` | The kill-oracle block — at least one oracle required (see [Kill oracles](#kill-oracles)) |
| `verify.fpv_config` | Path to an `fpv.yaml`, relative to `mut.yaml` (FPV oracle) |
| `verify.verification` | Name of the verification in that `fpv.yaml` to use as the oracle — **required when** `fpv_config` is set |
| `verify.test_config` | Path to a `tests.yaml`, relative to `mut.yaml` (simulation oracle) |
| `verify.tests` | Optional subset of test names to run; empty (default) runs every test in the suite |
| `verify.assertions` | Compile SVA in via Verilator `--assert`. Default `true` |
| `name` | Campaign identifier; used in `artefacts/mut/<name>/`. Defaults to `model` |
| `top` | Top module under test. Defaults to `model` |
| `budget.max_mutants` | Global ceiling on mutants generated across the campaign. Default `100`. Under a non-empty `scope` this is drained over the scoped files in sorted order, so later files may be truncated once it is spent (see [Scope: hierarchical designs](#scope-hierarchical-designs)) |
| `budget.per_file_cap` | Per-file cap (max mutants per scoped file), or `null` for none (default `null`) |
| `budget.time_budget_minutes` | Wall-clock budget in minutes, or `null` for none (default `null`) |
| `budget.schedule` | `"sequential"` (default) or `"round_robin"` |
| `scope.include` / `scope.exclude` | Optional shell-glob lists selecting which files participate. Empty (default) is a single-file campaign over `design_file`; non-empty ingests the design hierarchy (see [Scope: hierarchical designs](#scope-hierarchical-designs)) |

### Operators

The six operators map 1:1 onto `rtl_buddy_xeno.MutationKind`. An operator not recognised by the *installed* `rtl-buddy-xeno` is a fatal error at run time:

| Operator | Mutation |
|---|---|
| `arith_flip` | Flip an arithmetic operator (e.g. `+` ↔ `-`) |
| `bit_op_flip` | Flip a bitwise/logical operator (e.g. `&` ↔ `\|`) |
| `cond_negate` | Negate a condition |
| `cond_const` | Force a condition to a constant |
| `assign_drop` | Drop an assignment |
| `port_binding_swap` | Swap two port bindings on an instantiation |

## Scope: hierarchical designs

`scope.include` / `scope.exclude` select which files a campaign mutates. The two modes are distinguished purely by whether the block is empty:

- **Empty `scope` is the single-file default.** With no include/exclude, the campaign mutates exactly the one `design_file`. This is the path most leaf-block campaigns take, and it needs **no** rtl-buddy-view and no hierarchy graph — the empty-scope path is unchanged.
- **Non-empty `scope` ingests the design hierarchy.** rtl_buddy resolves the globs against the hierarchy graph that rtl-buddy-view emits (the same graph behind `rb hier --format json`), so when `scope` is set the rtl-buddy-view binary must be on `PATH`.

How the globs are matched (this is today's implementation, not an aspiration):

- **Shell-glob patterns, matched case-sensitively on every platform.** Patterns are stdlib `fnmatch` globs. There is **no** `**` recursion — spell out path segments explicitly (e.g. `top/sub/*.sv`, not `top/**/x.sv`).
- **Each pattern is matched against both a node's instance path and its source file.** An `include` glob can target either an instance path (e.g. `top.u_alu`) or a file (its absolute path *or* its model-relative path).
- **Empty `include` selects every in-scope node**; any `exclude` match drops a node. A scope that resolves to **zero files is a hard error** (`FatalRtlBuddyError`) — fix the patterns rather than silently mutating nothing.

What gets mutated, and where the baseline lives:

- **Mutation granularity is per file.** The scoped file-set is mutated one file at a time, with each mutant spliced into its origin file. A module instantiated twice maps to a single source file and is mutated **once**; per-instance scoping is a possible future extension.
- **`design_file` stays required and is the baseline-oracle target in both modes.** When `scope` is set it is the scoped file-set that gets mutated — `design_file` itself is **not** mutated, it remains the target the baseline oracle proves/runs against.
- **Budget under scope.** `budget.per_file_cap` caps mutants per scoped file; `budget.max_mutants` is a global ceiling drained over the scoped files in sorted order, so later files may be truncated once it is spent.

Scope graph-ingestion is the hierarchical-design work tracked under #206 / #239. Until per-instance scoping lands, scope narrows *which* files a campaign mutates rather than distinguishing two instances of the same module.

## Running

```bash
# List candidate mutation sites without mutating (uses ./mut.yaml)
rb mut list

# A specific config
rb mut list -c mut/demo/mut.yaml

# Generate mutants, score them against the oracles, and write a report
rb mut run -c mut/demo/mut.yaml

# Recompute the score from a saved report (no re-run)
rb mut score mut/demo/artefacts/mut/demo_top/mut_report.json
```

`rb mut run` builds each mutant in `debug` builder mode by default. All three subcommands take `-c`/`--mut-config` (default `mut.yaml`) and, like every command, anchor their artefact tree on the directory containing the selected `mut.yaml` — not your shell's cwd (see [Execution Context](execution-context.md)).

## Output

`rb mut list` prints a **Mutation Candidates (N)** table with `Operator`, `Line:Col`, and `Snippet` columns.

`rb mut run` prints a **Mutation Testing Results** table with one row per mutant:

| Column | Contents |
|---|---|
| `Mutant` | Mutant id |
| `Operator` | Operator that produced it |
| `Outcome` | `KILLED` / `SURVIVED` / `ERRORED` |
| `Verdict` | The oracle verdict recorded for the mutant (e.g. the FPV proof's `PASS`/`FAIL`/`NA`) |
| `Predicted Signals` | Signals the operator predicted it would perturb (`-` if none) |
| `Mutation` | Short diff summary of the injected change |

…followed by summary lines:

```
Mutation score: 78.6% (killed 11 / scored 14)
Survived: 3   Errored: 2   Baseline: PASS
Predicted-observable misses (weak properties): m07, m12
```

The full report is written to `<mut.yaml dir>/artefacts/mut/<campaign>/mut_report.json` and echoed as `Report written to …`. Its keys are `name`, `baseline_verdict`, `killed`, `survived`, `errored`, `score`, and `mutants[]` (each with `mutant_id`, `operator`, `outcome`, `verdict`, `diff_summary`, `predicted_signals`). `rb mut score <report>` recomputes the score from exactly this file.

### Machine output

Under `rb --machine …` (a global flag, before the subcommand):

- `rb --machine mut list` emits a `sites` array in the JSON envelope.
- `rb --machine mut run` / `rb --machine mut score` emit the full report under `report`.

### Exit codes

`rb mut run` exits `0` when it produced a scorable result and `1` only when nothing was scorable (`score` is `n/a`). **Score thresholding is not gated** — failing under a target score is a separate concern; the command does not exit non-zero just because the score is low. `rb mut list` and `rb mut score` exit `0` on success and fail only on fatal errors (missing engine, missing config, missing report).
