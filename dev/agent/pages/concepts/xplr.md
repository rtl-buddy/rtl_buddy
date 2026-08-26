---
description: Record, compare, reproduce, and prune design-space experiments with the `rb xplr` ledger.
---

# Design-Space Exploration (`rb xplr`)

`rb xplr` records design-space experiments as a source revision, declared knob changes, rationale, and outcome. It does not choose changes or run real flows; use it to preserve the evidence an agent or engineer needs to continue a search.

## Run the experiment lifecycle

Each experiment has one registration and one terminal outcome:

```bash
rb --machine xplr register --json manifest.json
# Run rb synth, rb fpga, a vendor flow, or another evaluator.
rb --machine xplr attach-outcome exp-0001 --json outcome.json
```

`register` allocates an `exp-NNNN` id, pins the source, records the declared changes, and writes `outcome.status: pending`. `attach-outcome` accepts `success` or `failed`; replacing a terminal outcome requires `--force`.

Use `failed` only when the flow did not complete. A completed but infeasible design point is `success` with `routed: false`, which keeps it out of the Pareto frontier without losing its measurements.

Records live at `<project root>/artefacts/xplr/<id>/record.json` regardless of the invocation directory. Ledger writes use their own lock and do not contend with suite artefact locks.

## Declare an experiment

A useful manifest records both the delta from its parent and the complete resolved state:

```json
{
  "parent": "exp-0002",
  "hypothesis": "Reducing FIFO depth should improve delay without increasing LUT use.",
  "knobs": [
    {
      "name": "fifo_depth",
      "from": 6,
      "to": 2,
      "layer": "flow",
      "rationale": "The previous unroll change exhausted the source-level delay improvement."
    }
  ],
  "config_snapshot": {
    "fifo_depth": 2,
    "unroll_factor": 1,
    "place.directive": "default"
  },
  "provenance": {
    "tools": [{"name": "vivado", "version": "2022.1"}],
    "agent": "timing-search"
  }
}
```

`knobs` is the change from `parent`; `config_snapshot` is the absolute state needed to reproduce or branch from the experiment. Knob values are arbitrary JSON scalars. The optional layer is `source`, `flow`, or `impl`.

Write one falsifiable sentence in `hypothesis`: what should move, in which direction, and why. Give every changed knob a rationale tied to an earlier experiment, report, or observation. Set `parent` to the experiment actually used as the starting point.

Input keys are strict. Unknown fields or schema violations exit 2 and name the invalid key and allowed alternatives.

## Pin the source revision

If `source.git_sha` is supplied in the manifest, xplr records it verbatim. Otherwise `cfg-xplr.commit-mode` controls pinning:

- `auto` records `HEAD` when the configured source scope is clean. If dirty, it snapshots the scope onto an `exp/<id>` branch without changing the working tree.
- `self-managed` rejects an uncommitted source scope.

`source.diff_from` defaults to the parent's pinned revision. Override it with `--baseline <ref>`.

The ledger directory, xplr worktree root, and `rtl_buddy.log` are excluded from source dirtiness and automatic snapshots. Agent scratch files are not; keep `artefacts/`, logs, worktrees, and temporary manifests gitignored. `register` warns when the ledger or log is inside a repository but not ignored.

## Attach an outcome

Declare terminal status, metrics, directions, units, and artefact paths:

```json
{
  "status": "success",
  "metrics": {
    "routed": true,
    "wns_ns": -0.21,
    "lut_pct": 71.4,
    "wall_clock_s": 900
  },
  "metric_meta": {
    "wns_ns": {"direction": "max", "unit": "ns"},
    "lut_pct": {"direction": "min", "unit": "%"}
  },
  "artifacts": ["post_route.dcp", "timing_summary.rpt"]
}
```

Metrics are numbers or booleans. Only numeric metrics with a `min` or `max` direction participate in Pareto dominance. Undirected measurements remain available for reporting and comparison.

## Read the ledger: frontier, diff, knob-effect

Use machine mode so the result is a single stable JSON envelope:

```bash
rb --machine xplr list
rb --machine xplr show exp-0003
rb --machine xplr frontier
rb --machine xplr diff exp-0002 exp-0003
rb --machine xplr knob-effect fifo_depth
```

- `list` returns compact experiment summaries.
- `show` returns the complete record, including the reproducible `config_snapshot`.
- `frontier` separates non-dominated, dominated, infeasible, and excluded experiments. Use `--metrics name:min,...` to override directions and `--prefer` to sort the frontier without dropping points.
- `diff` compares knob manifests, direction-aware outcome deltas, and pinned Git revisions. Add `--patch` for the source diff.
- `knob-effect` reports each declared change to one knob and its metric delta from the parent. An unknown knob returns an empty effect list plus known names and suggestions.

An empty frontier with populated `excluded` usually means successful experiments lack directed metrics. Fix `metric_meta` before drawing optimization conclusions.

## Use the exploration loop

Repeat this decision cycle:

1. Read `frontier`; use `show` to recover a candidate's absolute configuration.
2. Check `knob-effect` before retrying a knob and `diff` when comparing neighbours.
3. Form one hypothesis and apply one interpretable change outside xplr.
4. Register the experiment with its parent, delta, rationale, and snapshot.
5. Run the real flow and attach the terminal outcome with directed metrics.
6. Read the updated frontier and continue.

The ledger is the shared state, so another agent or machine can continue from the same records.

## Materialize revisions and reclaim disk

Every pinned experiment can be recreated as a detached worktree:

```bash
rb xplr materialize exp-0003
rb xplr release exp-0003
rb xplr gc --dry-run
rb xplr gc --policy keep-frontier --target-gb 40
```

`materialize` is idempotent and defaults to `artefacts/xplr/worktrees/<id>`. `release` removes that worktree but keeps the source branch and experiment record.

Garbage collection always preserves `record.json`. The default `keep-frontier` policy also protects frontier members, their direct lineage, and non-terminal experiments; eligible worktrees and listed outcome artefacts are removed oldest-first until usage is below the target. `register` automatically invokes the configured policy above the high watermark and blocks only when a hard-cap overrun cannot be reclaimed.

## Mockflow: a synthetic benchmark with known answers

Use `rb xplr mock` to test an exploration policy without EDA runtime:

```bash
rb --machine xplr mock info --scenario zdt1
rb --machine xplr mock run --scenario zdt1 --register
rb --machine xplr mock score --scenario zdt1
```

`mock info` returns knob domains, costs, infeasible combinations, and analytic ground truth. `mock run` deterministically evaluates a knob vector; `--noise` adds seeded objective noise and `--register` records the experiment and outcome together. Without `--register`, the payload's `outcome` can be passed directly to `attach-outcome`.

Available scenarios are `rastrigin` for single-objective WNS maximization and `zdt1` for LUT/delay minimization. `mock score` reports regret for a single objective or hypervolume and distance-to-front for multiple objectives.

When registering mock results outside a Git repository, provide `--source-sha` and optionally `--source-branch`.

## Configure xplr

All `cfg-xplr` keys are optional:

```yaml
cfg-xplr:
  commit-mode: auto
  source-scope: ["."]
  disk-high-watermark-gb: 50
  disk-hard-cap-gb: 80
  eviction-policy: keep-frontier
  worktree-root: artefacts/xplr/worktrees
```

`rb xplr` needs only a `root_config.yaml` or Git root; it does not load builder or platform configuration. When invoking it from elsewhere, anchor project discovery explicitly:

```bash
rb xplr --root /path/to/project frontier
```

See the [CLI reference](../reference/cli.md) for the full command surface.

## Record and machine contracts

Each `record.json` validates against the bundled draft-2020-12 schema `rtl_buddy/xplr/xplr-experiment-1.0.json`. The main blocks are `source`, `knobs`, optional `config_snapshot`, `outcome`, and `provenance`; the record also carries `schema_version`, id, optional parent, and hypothesis.

Every `rb --machine xplr ...` command prints one [machine envelope](../agents.md#machine-mode). Exit 0 means success. Exit 2 reports user or schema errors as `payload.error`. Optional payload keys may be added in minor releases; removing or changing record fields requires a schema-version change.
