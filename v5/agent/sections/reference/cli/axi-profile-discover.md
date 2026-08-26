## axi-profile discover

```text
Usage: rtl-buddy axi-profile discover [OPTIONS] MODEL_NAME                             
                                                                                        
 parse RTL to (re)generate the model's axi-bundles.yaml manifest                        
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    model_name      TEXT  model from models.yaml [required]                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --model-config  -c      TEXT  models.yaml to use [default: models.yaml]              │
│ --output        -o      TEXT  output path for axi-bundles.yaml (default: the model's │
│                               `axi_bundles:` from models.yaml when set, else         │
│                               artefacts/axi/<model>/axi-bundles.yaml)                │
│ --amend                 TEXT  existing axi-bundles.yaml to merge user edits from     │
│                               (deferred to a follow-up; warns if passed)             │
│ --tool                  TEXT  path to the axi-profiler binary                        │
│                               [default: axi-profiler]                                │
│ --help                        Show this message and exit.                            │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
