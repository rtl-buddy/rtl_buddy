## hier-query

```text
Usage: rtl-buddy hier-query [OPTIONS] NAME VERB ARG

 query the module hierarchy via rtl-buddy-view (find-module, subtree, instances-of,
 port-connections, source-snippet); JSON on stdout

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    name      TEXT  model name from models.yaml [required]                          │
│ *    verb      TEXT  query verb: find-module, subtree, instances-of,                 │
│                      port-connections, or source-snippet                             │
│                      [required]                                                      │
│ *    arg       TEXT  verb argument: a module name (find-module, instances-of) or a   │
│                      dot-separated instance path rooted at the model (subtree,       │
│                      port-connections, source-snippet)                               │
│                      [required]                                                      │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --model-config  -c                       TEXT     models.yaml to use                 │
│                                                   [default: models.yaml]             │
│ --frontend                               TEXT     parser frontend (verible|slang)    │
│ --format                                 TEXT     subtree only: json (default) or    │
│                                                   tree                               │
│ --context                                INTEGER  source-snippet only: context lines │
│                                                   on each side                       │
│ --line-numbers      --no-line-numbers             source-snippet only: prefix lines  │
│                                                   with source line numbers (default  │
│                                                   on)                                │
│                                                   [default: line-numbers]            │
│ --tool                                   TEXT     path to the rtl-buddy-view binary  │
│                                                   [default: rtl-buddy-view]          │
│ --help                                            Show this message and exit.        │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
