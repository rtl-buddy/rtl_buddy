## phys summary

```text
Usage: rtl-buddy phys summary [OPTIONS]

 the run's totals, its heaviest modules and its hottest instances

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --limit           INTEGER RANGE [x>=0]  rows per ranking, heaviest/hottest first (0  │
│                                         for all); truncates the --machine payload    │
│                                         too                                          │
│                                         [default: 10]                                │
│ --phys-dir        TEXT                  artefact directory holding                   │
│                                         phys-manifest.json                           │
│                                         [default: (newest phys-manifest.json under   │
│                                         the project root)]                           │
│ --manifest        TEXT                  phys-manifest.json to read directly          │
│ --help                                  Show this message and exit.                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
