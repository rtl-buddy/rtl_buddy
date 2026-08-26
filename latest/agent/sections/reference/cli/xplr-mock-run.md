## xplr mock run

```text
Usage: rtl-buddy xplr mock run [OPTIONS]

 evaluate one knob vector; with --register, record it as a ledger experiment with the
 outcome attached in one step

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ *  --scenario             TEXT     scenario name (rastrigin|zdt1) [required]         │
│    --json                 TEXT     JSON knob-value object {name: value}, or '-' for  │
│                                    stdin; omitted knobs take their scenario defaults │
│    --seed                 INTEGER  noise seed (irrelevant when --noise is 0)         │
│                                    [default: 0]                                      │
│    --noise                FLOAT    stddev of seeded Gaussian noise added to the      │
│                                    objective metrics (simulated run-to-run variance; │
│                                    default 0 = exact)                                │
│                                    [default: 0.0]                                    │
│    --register                      register a ledger experiment AND attach the       │
│                                    outcome in one step (knobs recorded as            │
│                                    from=scenario default)                            │
│    --source-sha           TEXT     with --register: record this sha verbatim as      │
│                                    source.git_sha (the agent-declared pin path; no   │
│                                    dirty bit). The escape hatch for sandboxes where  │
│                                    the project root is not a git repository          │
│    --source-branch        TEXT     with --source-sha: optional source.branch label,  │
│                                    recorded verbatim                                 │
│    --help                          Show this message and exit.                       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
