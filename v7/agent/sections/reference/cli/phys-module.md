## phys module

```text
Usage: rtl-buddy phys module [OPTIONS] MODULE

 one module's cells and area, and the instances of it with power

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    module      TEXT  module or liberty cell as the model records it [required]     │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --limit           INTEGER RANGE [x>=0]  instances to list, hottest first (0 for      │
│                                         all); truncates the --machine payload too    │
│                                         [default: 10]                                │
│ --phys-dir        TEXT                  artefact directory holding                   │
│                                         phys-manifest.json                           │
│                                         [default: (newest phys-manifest.json under   │
│                                         the project root)]                           │
│ --manifest        TEXT                  phys-manifest.json to read directly          │
│ --help                                  Show this message and exit.                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
