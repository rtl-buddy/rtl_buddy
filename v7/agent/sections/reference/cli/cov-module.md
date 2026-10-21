## cov module

```text
Usage: rtl-buddy cov module [OPTIONS] MODULE

 per-file, per-point coverage for one module's sources

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    module      TEXT  module name as the coverage model records it [required]       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --cold        --all             list only the points with no hits [default: cold]    │
│ --limit                INTEGER  points to list per metric (0 for all) [default: 20]  │
│ --cov-dir              TEXT     coverage artefact directory to read                  │
│                                 [default: (newest cov_dir under the project root)]   │
│ --manifest             TEXT     manifest.json to read directly                       │
│ --help                          Show this message and exit.                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
