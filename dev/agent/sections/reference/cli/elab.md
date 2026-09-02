## elab

```text
Usage: rtl-buddy elab [OPTIONS] [MODEL_NAME]

 elaborate a model with pyslang

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   model_name      [MODEL_NAME]  model to elaborate; required unless --list is used   │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --models-config  -c      TEXT     models.yaml to use [default: models.yaml]          │
│ --profile                TEXT     named elaboration profile                          │
│ --list                            list models and named profiles, then exit          │
│ --dispatch               TEXT     execution backend (local, local-parallel, slurm)   │
│ --jobs           -j      INTEGER  local-parallel process count                       │
│ --help                            Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
