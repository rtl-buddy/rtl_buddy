## hub send diagnose

```text
Usage: rtl-buddy hub send diagnose [OPTIONS] SOURCE [ITEMS]...

 Push a diagnostics_set bundle for SOURCE. Each ITEM is
 <file>:<line>:<severity>:<code>:<message>. --clear sends an empty set, clearing
 SOURCE's diagnostics. --instance attaches a view.json instance_path so consumers skip
 file-and-line resolution.

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    source      TEXT        producer key, e.g. 'analysis-tool'; a new push replaces │
│                              the previous one for the same key                       │
│                              [required]                                              │
│      items       [ITEMS]...  <file>:<line>:<sev>:<code>:<msg> ...                    │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --clear                 Send an empty items list (clears SOURCE).                    │
│ --instance        TEXT  view.json instance_path to attach to every ITEM in this      │
│                         push.                                                        │
│ --help                  Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
