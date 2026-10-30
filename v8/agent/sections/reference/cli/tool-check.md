## tool-check

```text
Usage: rtl-buddy tool-check [OPTIONS]

 check installed tool dependencies and subcommand readiness

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --format                                       TEXT  text | json [default: text]     │
│ --required-for                                 TEXT  check only what `rb             │
│                                                      <subcommand>` needs             │
│ --explain                                      TEXT  show install instructions for a │
│                                                      single tool and exit            │
│ --strict                                             exit non-zero if any required   │
│                                                      tool is missing/outdated        │
│ --include-optional    --no-include-optional          include optional tools          │
│                                                      (default: yes)                  │
│                                                      [default: include-optional]     │
│ --probe-versions      --no-probe-versions            run `<tool> --version` to       │
│                                                      capture installed version       │
│                                                      (default: yes)                  │
│                                                      [default: probe-versions]       │
│ --help                                               Show this message and exit.     │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
