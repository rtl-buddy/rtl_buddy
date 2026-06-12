---
description: The rb xplr experiment ledger for design-space exploration — experiment lifecycle, record schema, the machine-mode JSON contract agents rely on, rationale conventions, the mockflow benchmark harness, and a worked agent loop.
---

# Design-Space Exploration (rb xplr)

`rb xplr` is an LLM-native, tool-agnostic **experiment ledger** for design-space exploration. The agent (or human) drives the knobs — RTL parameters, partition assignments, tool directives — and *declares* what it changed; `rb xplr` records the declaration, pins the exact source revision, captures the flow's outcome metrics, and curates the Pareto frontier across experiments.

`rb xplr` is a bookkeeper, **not** an optimizer: it never proposes the next experiment, never runs a flow (the synthetic [mockflow](#mockflow-a-synthetic-benchmark-with-known-answers) dev harness is the only exception), and never owns a knob taxonomy. The experiment unit is:

```
(git-pinned source + agent-declared knob manifest) -> outcome
```

That division of labor is deliberate. The agent owns the reasoning — what to try and why. The ledger owns the memory — what was tried, on exactly which source, and what happened — so the search history survives context windows, crashes, and handoffs between agents.

## Experiment lifecycle

An experiment moves through exactly one write pair:

1. **`rb xplr register --json manifest.json`** opens the experiment: allocates the next id, pins the source revision under the configured commit policy (see [Source pinning](#source-pinning)), records the knob manifest, and writes the record with `outcome.status: "pending"`.
2. The flow runs **outside rb xplr** — `rb synth`, `rb pnr`, a vendor tool, anything.
3. **`rb xplr attach-outcome <id> --json outcome.json`** declares the terminal result: `status` must be `success` or `failed`, plus metrics. Re-attaching to an already-terminal experiment requires `--force`.

Everything else is read-only curation: `list`, `show`, `frontier`, `diff`, `knob-effect`, `mock score`.

Experiment ids auto-increment: `exp-0001`, `exp-0002`, ... (id pattern `^[A-Za-z0-9._-]+$`). The ledger is one directory per experiment at the **project root** — `artefacts/xplr/<exp-id>/record.json` — no matter which suite directory you invoked `rb` from, so a project has exactly one ledger. Write commands take a lock on the ledger root only; they never contend with a running flow's suite artefact lock.

A note on `status` semantics: `failed` means **the flow itself broke** (crash, license, elaboration error). A run that completed but produced an infeasible design point — it did not route, it failed timing closure hopelessly — is `success` with a boolean metric `routed: false`; the analysis layer treats that as the infeasibility marker and keeps it off the frontier.

## The record schema

Every record is validated against a strict (`additionalProperties: false`) JSON Schema, draft 2020-12, version `1.0`. The schema ships inside the wheel at `rtl_buddy/xplr/xplr-experiment-1.0.json`; malformed input fails loudly with exit code 2 naming exactly what was wrong. The blocks:

| Block | Required | Content |
|---|---|---|
| `schema_version` | yes | `"1.0"` |
| `id` | yes | ledger-allocated `exp-NNNN` |
| `parent` | no | id of the experiment this one was derived from — powers `knob-effect` deltas |
| `hypothesis` | no | one sentence: what this experiment is expected to show |
| `source` | yes | `git_sha` (required, 7-40 hex), `branch`, `diff_from`, `dirty` |
| `knobs` | yes | array of `{name, from, to, rationale?, layer?}` — the agent-declared **delta vs the parent**, values untyped |
| `config_snapshot` | no | opaque object: the full **absolute** resolved knob state for re-run; rb xplr never interprets it |
| `outcome` | yes | `status` (`pending`/`running`/`success`/`failed`), `metrics` (numbers/booleans), `metric_meta` (`{direction?: min\|max, unit?}` per metric), `artifacts` (paths) |
| `provenance` | yes | `created` (required), `tools` (`[{name, version}]`), `reused_state`, `agent` |

`knobs[].layer` is the only taxonomy rb xplr imposes, and it is coarse on purpose: `source` (RTL edit), `flow` (architecture/flow-level choice), `impl` (tool directive). A complete realistic record:

```json
{
  "schema_version": "1.0",
  "id": "exp-0007",
  "parent": "exp-0003",
  "hypothesis": "Moving blk_c off FB3 onto FB2 (SLR1) should relieve the FB2-FB3 SLL wall; expect SLL util down, watch for a Fmax hit on FB2.",
  "source": {
    "git_sha": "9f3a1c4",
    "branch": "exp/blk-c-to-fb2",
    "diff_from": "1b0e7a2",
    "dirty": false
  },
  "knobs": [
    {
      "name": "partition.blk_c.fpga",
      "from": "FB3",
      "to": "FB2",
      "rationale": "relieve FB2-FB3 SLL crossing that was the binding constraint in exp-0003",
      "layer": "flow"
    },
    {
      "name": "vivado.place.directive",
      "from": "Default",
      "to": "Explore",
      "rationale": "FB2 now denser; give the placer more freedom",
      "layer": "impl"
    }
  ],
  "config_snapshot": {
    "partition": { "blk_a": "FB1", "blk_b": "FB2", "blk_c": "FB2", "blk_d": "FB4" },
    "vivado": { "place_directive": "Explore", "route_directive": "Default", "seed": 3 }
  },
  "outcome": {
    "status": "success",
    "metrics": {
      "routed": true,
      "sll_util_fb2_fb3": 0.58,
      "wns_ns_fb2": -0.142,
      "lut_pct_fb2": 71.4,
      "wall_clock_s": 1290
    },
    "metric_meta": {
      "sll_util_fb2_fb3": { "direction": "min", "unit": "fraction" },
      "wns_ns_fb2": { "direction": "max", "unit": "ns" },
      "lut_pct_fb2": { "direction": "min", "unit": "percent" }
    },
    "artifacts": ["FB2/post_route.dcp", "FB2/timing_summary.rpt"]
  },
  "provenance": {
    "created": "2026-06-11T15:11:03+08:00",
    "tools": [
      { "name": "protocompiler", "version": "V-2023.12-1" },
      { "name": "vivado", "version": "2022.1" }
    ],
    "reused_state": "sg0",
    "agent": "claude-opus-4-8"
  }
}
```

## Machine-mode contract

Every `rb xplr` command works with the global `--machine` flag and prints a **single JSON envelope on stdout**:

```json
{"command": "xplr frontier", "exit_code": 0, "meta": {"rtl_buddy_version": "...", "argv": [...], "cwd": "...", "git": {...}}, "payload": { ... }}
```

The shapes documented on this page are the `payload` member. Exit codes: **0** success; **2** fatal user error (`FatalRtlBuddyError` — malformed JSON input, unknown keys, schema violation, unknown experiment id), in which case the envelope's payload is `{"error": "<message>"}`. Error messages name the offending key and the allowed alternatives, so an agent can self-correct without docs access.

JSON **input** goes through `--json <file|->` options (`-` reads stdin). Inputs are checked strictly: unknown keys fail with the allowed-key list, and the assembled record is schema-validated before anything touches disk.

Payload stability: the keys shown on this page are the contract. New optional keys may appear in a minor release; existing keys are only removed or retyped with a record `schema_version` bump.

## Declaring a knob manifest (register input)

The `--json` document for `rb xplr register`:

```json
{
  "hypothesis": "one sentence: what you expect and why",
  "parent": "exp-0002",
  "knobs": [
    {
      "name": "fifo_depth",
      "from": 6,
      "to": 2,
      "layer": "flow",
      "rationale": "after exp-0002 the unroll lever is exhausted; depth-6 FIFOs are the only non-minimal numeric knob left"
    }
  ],
  "config_snapshot": { "your": "full resolved knob state" },
  "source": { "git_sha": "optional agent-owned pin" },
  "provenance": { "tools": [{ "name": "mockflow", "version": "1.0" }], "agent": "your-agent-id" }
}
```

All keys are optional; unknown keys are rejected. `knobs[].from`/`to` are untyped JSON scalars — `rb xplr` records them verbatim. The success payload is `{id, record_path, record}` with the full validated record (`outcome.status` is `"pending"`).

`knobs` is the **delta vs the parent**; `config_snapshot` is the **absolute state**. Declare both: the delta (with rationale) is what makes `diff` and `knob-effect` meaningful, and the snapshot is what lets anyone — including a later agent reading the ledger cold — re-run or branch from this exact point without replaying the whole knob history.

### Source pinning

If the manifest declares `source.git_sha` it is taken verbatim — the agent owns that pin. Otherwise the `cfg-xplr` commit policy applies: in the default `auto` mode a dirty source scope is snapshotted to an `exp/<id>` branch without disturbing your working tree (a clean scope just records `HEAD`); in `self-managed` mode an uncommitted scope is an error. Either way the recorded sha is exact and re-materializable. `source.diff_from` records the RTL-diff baseline; it defaults to the parent experiment's pinned sha, and `--baseline <ref>` overrides it.

rb's own bookkeeping never counts as source, even with the default `source-scope: ["."]`: the xplr ledger dir (`artefacts/xplr/`), the worktree root, and `rtl_buddy.log` are always excluded from both the dirtiness check and the snapshot — registering twice with identical RTL pins the same sha, and `xplr diff` then reports "both experiments pin the same source revision" instead of ledger noise. Still gitignore `artefacts/` and `rtl_buddy.log` (and any scratch files your agent writes, e.g. `manifest.json`): non-ignored bookkeeping clutters `git status`, ends up in your own commits, and agent scratch files *do* get snapshotted. `register` warns (`xplr.ledger_not_ignored`) when the ledger dir or log file is inside the repo and not gitignored.

## Declaring an outcome (attach-outcome input)

The `--json` document for `rb xplr attach-outcome <id>`:

```json
{
  "status": "success",
  "metrics": { "routed": true, "wns_ns": -0.21, "lut_pct": 71.4, "wall_clock_s": 900 },
  "metric_meta": {
    "wns_ns": { "direction": "max", "unit": "ns" },
    "lut_pct": { "direction": "min", "unit": "%" }
  },
  "artifacts": ["post_route.dcp", "timing_summary.rpt"],
  "provenance": { "tools": [{ "name": "vivado", "version": "2022.1" }], "reused_state": "sg0" }
}
```

`status` must be terminal (`success`/`failed`). Metric names are yours; values are numbers or booleans. Declare a `direction` in `metric_meta` for every metric that should participate in Pareto dominance — undirected metrics (like `wall_clock_s` above) are recorded and reported but never join dominance. The boolean `routed: false` convention marks a successful run whose design point is infeasible. The success payload is `{id, record_path, record}` with the merged record.

## Reading the ledger: frontier, diff, knob-effect

These are the views an agent reasons over. All payloads below are real outputs from the [worked example](#worked-example-an-agent-converging-on-mockflow) ledger.

**`rb --machine xplr frontier`** curates the non-dominated set over every successful, feasible experiment. Dominance runs over metrics that are numeric and have a declared direction (`metric_meta`, overridable with `--metrics name:min,name2:max`); `--prefer '0.7*lut_pct+0.3*delay_ns'` sorts the frontier by a weighted preference without dropping non-dominated points:

```json
{
  "metrics": [
    { "name": "delay_ns", "direction": "min", "unit": "ns" },
    { "name": "lut_pct", "direction": "min", "unit": "%" }
  ],
  "frontier": [
    { "id": "exp-0003", "metrics": { "routed": true, "wall_clock_s": 900.0, "lut_pct": 50.0, "delay_ns": 2.9289321881345245 } },
    { "id": "exp-0004", "metrics": { "routed": true, "wall_clock_s": 1140.0, "lut_pct": 10.0, "delay_ns": 6.83772233983162 } }
  ],
  "dominated": [
    { "id": "exp-0001", "dominated_by": ["exp-0002", "exp-0003", "exp-0004"] },
    { "id": "exp-0002", "dominated_by": ["exp-0003", "exp-0004"] }
  ],
  "infeasible": [],
  "excluded": []
}
```

`infeasible` lists `routed: false` ids; `excluded` lists `{id, reason}` for non-success or missing-metric experiments. An empty `frontier` plus populated `excluded` tells the agent its outcomes are missing directions, not that nothing works.

**`rb --machine xplr diff exp-0001 exp-0002`** answers "what exactly is different, and did it help?" — knob delta, direction-aware outcome delta, and the git diff between the pinned sources (`--patch` adds the full patch; unknown shas degrade to a `note`):

```json
{
  "a": "exp-0001",
  "b": "exp-0002",
  "knob_delta": {
    "added": [
      { "name": "unroll_factor", "from": 3, "to": 1, "rationale": "exp-0001 delay_ns=19.75 looks dominated by replicated logic depth; minimum unroll should shrink it", "layer": "source" }
    ],
    "changed": [],
    "reverted": [],
    "unchanged": [],
    "manifest_a": [],
    "manifest_b": [ { "name": "unroll_factor", "from": 3, "to": 1, "rationale": "...", "layer": "source" } ]
  },
  "outcome_delta": {
    "status_a": "success",
    "status_b": "success",
    "metrics": [
      { "name": "delay_ns", "a": 19.752451216018038, "b": 10.942235935955848, "delta": -8.81021528006219, "direction": "min", "assessment": "better" },
      { "name": "lut_pct", "a": 50.0, "b": 50.0, "delta": 0.0, "direction": "min", "assessment": "equal" },
      { "name": "wall_clock_s", "a": 60.0, "b": 660.0, "delta": 600.0, "direction": null, "assessment": "unknown" }
    ],
    "only_a": {},
    "only_b": {}
  },
  "source": {
    "a": { "git_sha": "92c706828b0883950ccbd8fb829a5a3f910887ea", "branch": "main", "diff_from": "92c706828b0883950ccbd8fb829a5a3f910887ea", "dirty": false },
    "b": { "git_sha": "92c706828b0883950ccbd8fb829a5a3f910887ea", "branch": "main", "diff_from": "92c706828b0883950ccbd8fb829a5a3f910887ea", "dirty": false },
    "stat": "",
    "note": "both experiments pin the same source revision"
  }
}
```

**`rb --machine xplr knob-effect unroll_factor`** is the per-knob history — every experiment that declared the knob, with the recorded rationale and the metric delta vs its parent. Consult it **before re-trying a knob**: the trail tells you what was already learned, by whom, and why:

```json
{
  "knob": "unroll_factor",
  "effects": [
    {
      "exp": "exp-0002",
      "status": "success",
      "from": 3,
      "to": 1,
      "rationale": "exp-0001 delay_ns=19.75 looks dominated by replicated logic depth; minimum unroll should shrink it",
      "metrics_after": { "routed": true, "wall_clock_s": 660.0, "lut_pct": 50.0, "delay_ns": 10.942235935955848 },
      "parent": "exp-0001",
      "metrics_parent_delta": { "delay_ns": -8.81021528006219, "lut_pct": 0.0, "wall_clock_s": 600.0 }
    }
  ]
}
```

A knob name that appears in **no** experiment's manifest still exits 0 with `effects: []` — an empty history is an answer, not an error — but the payload then also carries `known_knobs` (every distinct knob name declared anywhere in the ledger, sorted) and `suggestions` (close matches to the requested name), so a typo self-corrects in one round trip. When the knob was tried at least once, those keys are absent.

`rb --machine xplr list` returns `{experiments: [{id, status, git_sha, n_knobs, created, hypothesis?}]}` summaries; `rb --machine xplr show <id>` returns `{id, record_path, record}` with the full record — including `config_snapshot`, which is how an agent recovers the absolute knob state of a frontier point.

## Hypothesis and rationale conventions

The ledger is a **reasoning trail**, not just a value log. Two conventions make the difference between a pile of runs and a search history a future agent can continue:

- **Every experiment carries a `hypothesis`** — one falsifiable sentence: *what you expect to move, in which direction, and why*. Good: "Moving blk_c off FB3 should relieve the SLL wall; expect SLL util down, watch for a Fmax hit on FB2." Bad: "try another placement".
- **Every knob entry carries a `rationale`** — why *this* knob, *this* value, usually citing the evidence: a prior experiment id, a `knob-effect` observation, a report artifact. The rationale is surfaced verbatim by `diff` and `knob-effect`, which is exactly where the next decision gets made.

Set `provenance.agent` to a stable identifier for who/what made the decision, and `parent` to the experiment you branched from — `parent` is what turns isolated runs into lineages, and it is what `knob-effect` uses to compute deltas and what `gc` uses to protect frontier ancestry.

## The agent loop

The exploration loop an agent runs, end to end — see also the `rb xplr` section of the bundled skill:

1. **Read the state**: `rb --machine xplr frontier` (plus `xplr show <id>` for a frontier member's `config_snapshot`). On a fresh ledger, register a baseline first.
2. **Reason**: pick the frontier point to improve and the knob to move. Check `rb --machine xplr knob-effect <knob>` before re-trying a knob; use `rb --machine xplr diff <a> <b>` to compare a candidate against its frontier neighbor.
3. **Apply the change** — edit RTL, a config, a tool directive. This happens outside rb xplr.
4. **Declare it**: `rb --machine xplr register --json manifest.json` with `hypothesis`, `parent`, per-knob `rationale`, and `config_snapshot`. Note the returned `id`.
5. **Run the flow** (your real flow, or `rb xplr mock run` when dry-running the loop).
6. **Declare the result**: `rb --machine xplr attach-outcome <id> --json outcome.json` with directed `metric_meta`.
7. **Repeat** from 1. The frontier read in the next iteration reflects this experiment — improvements stick, regressions are recorded with their rationale so nobody retries them blind.

Nothing in the loop requires the same agent, the same session, or even the same machine: the ledger is the only shared state.

## Worked example: an agent converging on mockflow

A four-iteration loop against the `zdt1` scenario (two competing objectives: `lut_pct` min vs `delay_ns` min). Every payload below is a real command output, generated with deterministic settings (`--noise 0`, the default). First, discover the knob domains:

```console
$ rb --machine xplr mock info --scenario zdt1
```

The payload declares five knobs — `partition.cut` (float 0..1, flow), `unroll_factor` (int 1..9, source), `fifo_depth` (int 2..18, flow), and two choice knobs — plus the cost model and one infeasible combination. (It also contains `ground_truth`; a self-respecting agent does not read it — that is the answer key `mock score` grades against.)

**Iteration 1 — baseline.** Register-and-run the tool defaults in one step:

```console
$ rb --machine xplr mock run --scenario zdt1 --register
```

```json
{ "id": "exp-0001", "metrics": { "routed": true, "wall_clock_s": 60.0, "lut_pct": 50.0, "delay_ns": 19.752451216018038 } }
```

(abridged: the full payload carries the resolved knobs and the registered record). The frontier is now that single point.

**Iteration 2 — attack delay.** Hypothesis: unroll inflates logic without helping the cut. Register the delta with its reasoning, run the flow, attach the outcome:

```console
$ rb --machine xplr register --json - <<'EOF'
{
  "hypothesis": "unroll_factor inflates logic without helping the partition cut; expect delay_ns down at unchanged lut_pct",
  "parent": "exp-0001",
  "knobs": [
    { "name": "unroll_factor", "from": 3, "to": 1, "layer": "source",
      "rationale": "exp-0001 delay_ns=19.75 looks dominated by replicated logic depth; minimum unroll should shrink it" }
  ],
  "config_snapshot": { "scenario": "zdt1", "knobs": { "partition.cut": 0.5, "unroll_factor": 1, "fifo_depth": 6, "place.directive": "default", "route.strategy": "timing" } },
  "provenance": { "tools": [{ "name": "mockflow", "version": "1.0" }], "agent": "doc-walkthrough" }
}
EOF
```

Returns `{"id": "exp-0002", ...}` with `outcome.status: "pending"`. Run and attach — the `mock run` payload carries an `outcome` member shaped exactly as a valid `attach-outcome --json` input (`{status, metrics, metric_meta}`), so `outcome.json` is one extraction away:

```console
$ echo '{"unroll_factor": 1}' | rb --machine xplr mock run --scenario zdt1 --json - \
    | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["payload"]["outcome"]))' \
    > outcome.json   # or: jq .payload.outcome
$ rb --machine xplr attach-outcome exp-0002 --json outcome.json
```

`outcome.status` is always `"success"` — the synthetic flow ran to completion (an unroutable point is `routed: false`, not a failure). The agent may override `status` with its own judgment before attaching.

`delay_ns` drops 19.75 → 10.94 at unchanged `lut_pct` — the `diff exp-0001 exp-0002` payload shown in [Reading the ledger](#reading-the-ledger-frontier-diff-knob-effect) assesses exactly that (`"assessment": "better"`), and the hypothesis is confirmed.

**Iteration 3 — the same move on the next knob.** Hypothesis: the remaining gap is buffering; `fifo_depth` 6 → 2 (parent `exp-0002`, rationale "after exp-0002 the unroll lever is exhausted; depth-6 FIFOs are the only non-minimal numeric knob left"). Result: `delay_ns` 10.94 → **2.93** at `lut_pct` 50 — `exp-0003` now dominates everything so far.

**Iteration 4 — spread the front.** `knob-effect` shows `unroll_factor` and `fifo_depth` moved only `delay_ns`; the lever that trades the two objectives is `partition.cut`. Probe the low-area end: `partition.cut` 0.5 → 0.1 from `exp-0003`. Result: `lut_pct` 10, `delay_ns` 6.84 — not better than `exp-0003`, but not worse either: a second non-dominated point. The frontier payload after iteration 4 (shown in full above) has `exp-0003` and `exp-0004` on the front, with both earlier runs in `dominated` — the frontier moved outward twice and then grew sideways.

**Score it.** Because mockflow's Pareto front is analytic, the loop gets a grade:

```console
$ rb --machine xplr mock score --scenario zdt1
```

```json
{
  "scenario": "zdt1",
  "objective": "multi",
  "metrics": ["lut_pct", "delay_ns"],
  "n_experiments": 4,
  "n_feasible": 4,
  "reference_point": { "lut_pct": 110.0, "delay_ns": 110.0 },
  "nondominated": [
    { "id": "exp-0003", "lut_pct": 50.0, "delay_ns": 2.9289321881345245 },
    { "id": "exp-0004", "lut_pct": 10.0, "delay_ns": 6.83772233983162 }
  ],
  "hypervolume": 10550.755175118664,
  "front_hypervolume": 11766.160134393658,
  "hypervolume_ratio": 0.8967033471079282,
  "distance_to_front": 0.0
}
```

Four experiments, 89.7% of the analytic front's hypervolume, and both frontier points sit exactly on the true front (`distance_to_front: 0.0`). The same loop, scripted as a dumb coordinate-descent heuristic, is CI-tested in `tests/test_xplr_loop.py`: regret must fall on the single-objective scenario and hypervolume must grow here — the eval harness for anything that claims to explore.

## mockflow: a synthetic benchmark with known answers

`rb xplr mock` is the only flow rb xplr ships, and it is deliberately fake: EDA-flavored knobs in, EDA-flavored metrics out, instant and deterministic, backed by benchmark functions whose optimum is known analytically. Use it to dry-run the agent loop, develop analysis tooling, or benchmark an optimizer — without EDA turnaround.

- **`rb xplr mock info [--scenario s]`** — knob specs, cost model, infeasible combinations, and the analytic `ground_truth`.
- **`rb xplr mock run --scenario s [--json knobs] [--seed N] [--noise sigma] [--register]`** — evaluate one knob vector. `--register` writes the ledger experiment and attaches the outcome in one step; without it the evaluation is stateless (use register/attach yourself, as a real flow would — the payload's `outcome` member is a ready-made `attach-outcome --json` input, see the worked example). Metrics are a pure function of `(scenario, knobs, seed)`; `--noise` adds seeded Gaussian variance to the objective metrics only. With `--register` in a sandbox whose project root is **not a git repo**, pass `--source-sha <sha>` (and optionally `--source-branch <label>`): it follows the agent-declared-pin path — recorded verbatim, no dirty bit — exactly like a register manifest declaring `source.git_sha`.
- **`rb xplr mock score [--scenario s]`** — grade the ledger against the ground truth: **regret** (`|best_found - optimum|`) for single-objective scenarios, **hypervolume** vs the documented reference point plus **distance-to-front** for multi-objective ones.

Two scenarios ship: `rastrigin` (single-objective, `wns_ns` max — a lattice of local optima around one global optimum at the numeric-knob midpoints) and `zdt1` (multi-objective, `lut_pct`/`delay_ns` min/min with an analytic Pareto front). Both have one infeasible categorical combination (`routed: false`) and a layer-based cost model: every knob moved off its default adds its layer cost to `wall_clock_s` (source 600s, flow 240s, impl 60s) — touching RTL is 10x the cost of a tool directive, so a cost-aware agent should prefer impl-layer probes when exploring.

## Worktrees and disk: materialize, release, gc

Because every experiment pins an exact sha (auto-snapshotted to an `exp/<id>` branch when the tree was dirty), any experiment can be rebuilt later:

- **`rb xplr materialize <id> [--path P]`** — create (or reuse, idempotently) a detached git worktree at the experiment's pinned sha, by default under `artefacts/xplr/worktrees/<id>`. Payload: `{id, git_sha, path, created, reused}`.
- **`rb xplr release <id>`** — remove the worktree; the branch and record are kept. Payload: `{id, path, removed}`.
- **`rb xplr gc [--dry-run] [--policy p] [--target-gb N]`** — reclaim disk, non-interactively. The default `keep-frontier` policy never evicts frontier members, their direct lineage, or non-terminal experiments; everything else goes oldest-first until usage is under the target (default: the configured high watermark). Eviction removes worktrees and `outcome.artifacts` files but **always keeps `record.json`** — an evicted experiment can be re-materialized and replayed. Payload: `{policy, usage_bytes_before, target_bytes, evicted: [{id, bytes_freed}], bytes_freed_total, protected, notes}`. `register` runs the same backstop automatically: over the high watermark it triggers gc by policy, and only an unfreeable hard-cap overrun blocks a new experiment.

## Configuration: cfg-xplr

One optional block in `root_config.yaml`; every key has a default:

```yaml
cfg-xplr:
  commit-mode: "auto"                # auto | self-managed
  source-scope: ["."]                # what auto-commit snapshots / dirt-checks
                                     # (rb bookkeeping — the xplr ledger dir,
                                     # worktrees, rtl_buddy.log — is always excluded)
  disk-high-watermark-gb: 50         # gc trigger
  disk-hard-cap-gb: 80               # backstop that blocks new registers
  eviction-policy: "keep-frontier"   # keep-frontier | oldest-first | manual
  worktree-root: "artefacts/xplr/worktrees"   # keep it gitignored
```

`rb xplr` reads this block without loading the rest of the root config, so it works in any project with a `root_config.yaml` or a git root — no builder or platform setup required. To drive a ledger from outside its project checkout, anchor the discovery explicitly with the group-level `--root` option: `rb xplr --root <project> <subcommand> ...`.
