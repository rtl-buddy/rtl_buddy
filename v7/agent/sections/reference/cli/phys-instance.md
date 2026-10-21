## phys instance

```text
Usage: rtl-buddy phys instance [OPTIONS] PATH

 one instance's power, or the rolled-up subtree under its path

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    path      TEXT  instance path, exact or the root of a subtree [required]        │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --limit           INTEGER RANGE [x>=0]  hottest children to list (0 for all);        │
│                                         truncates the --machine payload too          │
│                                         [default: 10]                                │
│ --phys-dir        TEXT                  artefact directory holding                   │
│                                         phys-manifest.json                           │
│                                         [default: (newest phys-manifest.json under   │
│                                         the project root)]                           │
│ --manifest        TEXT                  phys-manifest.json to read directly          │
│ --help                                  Show this message and exit.                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
