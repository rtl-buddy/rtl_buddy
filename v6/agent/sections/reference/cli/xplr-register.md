## xplr register

```text
Usage: rtl-buddy xplr register [OPTIONS]

 open a new experiment: pin the current git ref, record the agent-declared knob
 manifest, return its experiment id

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --json            TEXT  JSON manifest file, or '-' for stdin: {knobs: [{name, from,  │
│                         to, rationale?, layer?}], hypothesis?, parent?,              │
│                         config_snapshot?, source?: {git_sha?, branch?, diff_from?},  │
│                         provenance?: {tools?, agent?}}                               │
│ --baseline        TEXT  git ref to record as source.diff_from (the RTL-diff          │
│                         baseline). Default: the parent experiment's pinned sha when  │
│                         'parent' is given, else HEAD before any snapshot             │
│ --help                  Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
