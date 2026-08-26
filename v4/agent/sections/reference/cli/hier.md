## hier

```text
Usage: rtl-buddy hier [OPTIONS] MODEL_NAME                                             
                                                                                        
 render module hierarchy via rtl-buddy-view                                             
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    model_name      TEXT  model from models.yaml [required]                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --model-config     -c      TEXT  models.yaml to use [default: models.yaml]           │
│ --format                   TEXT  output format: tree, dot, mermaid, json             │
│                                  [default: tree]                                     │
│ --output           -o      TEXT  write renderer output to file instead of stdout     │
│ --frontend                 TEXT  parser frontend (verible|slang)                     │
│ --cdc-annotations          TEXT  clock-domain map JSON from `rtl-buddy-cdc           │
│                                  --emit-domain-map`                                  │
│ --rdc-annotations          TEXT  reset-domain map JSON from `rtl-buddy-cdc           │
│                                  --emit-reset-domain-map`                            │
│ --clock-legend                   dot format only: emit a side legend of clock colors │
│ --tool                     TEXT  path to the rtl-buddy-view binary                   │
│                                  [default: rtl-buddy-view]                           │
│ --help                           Show this message and exit.                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
