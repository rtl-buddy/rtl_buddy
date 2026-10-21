## xplr attach-outcome

```text
Usage: rtl-buddy xplr attach-outcome [OPTIONS] EXP

 attach flow-declared outcome metrics to an experiment (pending/running ->
 success|failed)

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    exp_id      EXP  experiment id, e.g. exp-0001 [required]                        │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ *  --json         TEXT  JSON outcome file, or '-' for stdin: {status:                │
│                         'success'|'failed', metrics?, metric_meta?, artifacts?,      │
│                         provenance?: {tools?, reused_state?}}                        │
│                         [required]                                                   │
│    --force              overwrite an outcome that is already terminal                │
│                         (success/failed)                                             │
│    --help               Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
