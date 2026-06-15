---
name: rtl-buddy
description: Use rtl_buddy to orchestrate SystemVerilog compile/sim workflows, randomized tests, regressions, synthesis, place-and-route, CDC lint, formal property verification, design-space exploration experiments, filelist generation, and verible checks. Trigger this skill when asked to run or debug rtl_buddy commands or interpret root_config.yaml, tests.yaml, models.yaml, regression.yaml, synth.yaml, synth_regression.yaml, pnr.yaml, cdc.yaml, fpv.yaml, or mut.yaml.
---

# rtl_buddy

Run `rtl-buddy --version` at the top of every run summary.

Use this skill for agent-specific workflow rules only. For command syntax or schema details, start with `rb --help`, `rb <subcommand> --help`, `rtl-buddy docs list`, `rtl-buddy docs show agents`, and `rtl-buddy --machine docs show reference/yaml`. When behavior surprises you (silent failures, paths landing in the wrong place, hook cwd), check `rtl-buddy docs show known-issues` first.

## Always use `--machine`

All agent invocations must use `--machine`: `rtl_buddy.log` becomes JSONL, and structured result commands (including `rb docs list`, excluding `rb docs show`) print a single JSON envelope to **stdout** — parse this for results. Envelope schema, JSONL log format, and exit codes (0 pass, 1 test failures, 2 fatal): `rtl-buddy docs show agents`.

## YAML map

Exact fields: `rtl-buddy --machine docs show reference/yaml`.

- `root_config.yaml` configures platforms, builders, coverage, waveform, synth, P&R, CDC, FPV, and default regression paths.
- `tests.yaml` is suite-local and defines testbenches plus tests; invoke from anywhere with `-c <path>` (outputs anchor on the config dir — see Execution context below).
- `models.yaml` defines design filelists referenced by tests, synth, CDC, and FPV.
- `synth.yaml`, `pnr.yaml`, `power.yaml`, `cdc.yaml`, and `fpv.yaml` define named runs for those flows; `regression.yaml`, `synth_regression.yaml`, `cdc_regression.yaml`, and `fpv_regression.yaml` are repo-level suite lists.
- `mut.yaml` defines one mutation-testing campaign for `rb mut`; `specs.yaml` feeds `rb spec` traceability commands.

## Pass/fail detection

- UVM uses configured warning/error thresholds; cocotb uses `cocotb_results.xml`, not `PASS`/`FAIL` stdout markers.
- Other sims should emit `PASS` or `FAIL` in `artefacts/<test>/test.log` (add an `ERR:` or `FAT:` line on failure); formal runs use `artefacts/<run>/sby_workdir/status` as the authoritative verdict when present.

## Formal property authoring

- `mode: prove` is k-induction up to `depth`: a true property can still report **`UNKNOWN`** if it is not an *inductive invariant*. In the induction step every `assert P` plays a dual role — proof obligation at step `k`, **and** constraint on the prior `k` states — so the two preferred fixes (per the YosysHQ SBY FAQ) are: strengthen the property's own hypothesis to exclude bad predecessors (`cnt <= 5`, not `cnt != 26`), or **add a companion assertion that marks an unreachable predecessor state bad** so other properties can use it as a constraint. Raising `depth` is sound but fragile; reach for it last. Keep a known-non-inductive case in regression with `xfail`/`xfail_strict`. Details: `rtl-buddy docs show concepts/fpv`.

## Mutation testing

- `rb mut list|run|score` drive a campaign from `mut.yaml` (needs `rtl-buddy-xeno`; a non-empty `scope` also needs `rtl-buddy-view` on `PATH`). Score is `killed / (killed + survived)`; errored mutants are dropped; survivors are verification holes. Details: `rtl-buddy docs show concepts/mut`.

## rb xplr (design-space exploration)

- `rb xplr` is the experiment ledger for DSE loops: it records what YOU changed, pins the source sha, and curates the Pareto frontier. It never proposes the next experiment — you do.
- The loop: `rb --machine xplr frontier` (+ `xplr show <id>` for a member's `config_snapshot`) -> reason -> apply the change (RTL/tool knob, outside rb) -> `rb --machine xplr register --json manifest.json` -> run the flow -> `rb --machine xplr attach-outcome <id> --json outcome.json` -> repeat.
- Always declare an experiment `hypothesis` and a per-knob `rationale`, plus `parent` — the ledger is a reasoning trail. Run `rb --machine xplr knob-effect <knob>` before re-trying a knob and `rb --machine xplr diff <a> <b>` to compare candidates.
- Declare `direction` in `metric_meta` for every metric that should join dominance; report a completed-but-unroutable point as `status=success` with `routed: false` (`failed` means the flow crashed).
- Dry-run the whole loop with `rb xplr mock run --scenario rastrigin|zdt1` and grade yourself with `rb xplr mock score`. Contract + worked example: `rtl-buddy docs show concepts/xplr`.

## Execution context

Outputs anchor on the **config file**, not your shell's cwd. `rb test -c path/to/tests.yaml` puts `artefacts/<test>/...` and `rtl_buddy.log` under `dirname(tests.yaml)`; same rule for `synth.yaml`, `cdc.yaml`, `fpv.yaml`, `pnr.yaml`, `power.yaml`, `models.yaml`. For `regression`, each suite anchors on its own `tests.yaml`; orchestration log under `regression.yaml`. Explicit CLI input/output paths (`-o out.svg`, `rb filelist <model> out.f`) follow shell semantics — relative to your cwd. Discover multi-suite layouts with `rg --files -g '**/tests.yaml'`; summarize per suite. Reference: `rtl-buddy docs show concepts/execution-context`.

## Artefacts and waveforms

- `artefacts/<test>/test.log`, `test.err`, `test.randseed`, `coverage.dat` — sim outputs for one run (`artefacts/<test>/run-0001/...` per iteration for `randtest`).
- `rb wave <test>` opens `artefacts/<test>/dump.fst` (FST from debug-mode builds, `-M debug`) in Surfer, running a debug sim first if no FST exists; needs `cfg-surfer` in `root_config.yaml` (`rtl-buddy docs show concepts/root-config`).
- With a hub running, curate the open wave view from the CLI: `rb hub send wave-items` (list), then `wave-add` / `wave-remove` / `wave-move` / `wave-comment`; each reports success/error (non-zero exit on a surfer rejection). See `rtl-buddy docs show concepts/hub`.
- Next docs: `rtl-buddy docs show reference/cli`, `rtl-buddy docs show reference/yaml`, `rtl-buddy docs show known-issues`
