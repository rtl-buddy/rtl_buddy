## xplr

```text
Usage: rtl-buddy xplr [OPTIONS] COMMAND [ARGS]...

 design-space exploration experiment ledger (agent-facing)

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --root        TEXT  anchor project-root discovery at this path instead of the        │
│                     current directory. Group-level: place it between 'xplr' and the  │
│                     subcommand, e.g. `rb xplr --root <project> list`                 │
│ --help              Show this message and exit.                                      │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ register        open a new experiment: pin the current git ref, record the           │
│                 agent-declared knob manifest, return its experiment id               │
│ attach-outcome  attach flow-declared outcome metrics to an experiment                │
│                 (pending/running -> success|failed)                                  │
│ list            list experiments in the ledger (one summary row each)                │
│ show            show one experiment's full record                                    │
│ diff            pairwise experiment diff: knob delta, direction-aware outcome delta, │
│                 and the git diff between the pinned sources                          │
│ frontier        curate the Pareto frontier (non-dominated set) over the declared     │
│                 numeric outcome metrics; dominated, infeasible (routed=false), and   │
│                 excluded experiments are reported alongside                          │
│ knob-effect     per-knob effect history: every experiment that declared the knob,    │
│                 with metric deltas vs its parent when available                      │
│ materialize     check the experiment's pinned sha out into its own git worktree      │
│                 (isolated build dir; disposable — the branch is the durable          │
│                 artifact). Idempotent                                                │
│ release         remove the experiment's worktree (worktree remove + prune); the exp  │
│                 branch and the ledger record are kept                                │
│ gc              reclaim experiment disk space non-interactively: evict heavy         │
│                 artifacts and worktrees per policy (default keep-frontier spares     │
│                 Pareto-frontier members and their lineage); record.json and the      │
│                 pinned sha survive, so evicted experiments can be re-materialized    │
│ mock            synthetic DSE backend with known optima (dev/CI harness)             │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
