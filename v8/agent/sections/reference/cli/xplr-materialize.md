## xplr materialize

```text
Usage: rtl-buddy xplr materialize [OPTIONS] EXP

 check the experiment's pinned sha out into its own git worktree (isolated build dir;
 disposable — the branch is the durable artifact). Idempotent

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    exp_id      EXP  experiment id, e.g. exp-0001 [required]                        │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  worktree location (default: <worktree-root>/<exp>/,              │
│                     worktree-root from cfg-xplr, under artefacts/ — keep it          │
│                     gitignored)                                                      │
│ --help              Show this message and exit.                                      │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
