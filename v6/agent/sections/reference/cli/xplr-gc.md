## xplr gc

```text
Usage: rtl-buddy xplr gc [OPTIONS]

 reclaim experiment disk space, non-interactively: evict heavy artifacts + worktrees
 per policy (default keep-frontier never touches Pareto-frontier members or their
 lineage); record.json and the pinned sha always survive, so evicted experiments can be
 re-materialized

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --dry-run                 report what would be evicted without touching anything     │
│ --policy           TEXT   eviction policy for this run: keep-frontier (default;      │
│                           frontier members + lineage are never evicted) |            │
│                           oldest-first | manual (list candidates, evict nothing)     │
│ --target-gb        FLOAT  gc down to this usage (default: cfg-xplr                   │
│                           disk-high-watermark-gb)                                    │
│ --help                    Show this message and exit.                                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
