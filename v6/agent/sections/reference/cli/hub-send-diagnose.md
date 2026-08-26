## hub send diagnose

```text
Usage: rtl-buddy hub send diagnose [OPTIONS] SOURCE [ITEMS]...

 Push a diagnostics_set bundle for SOURCE. Each ITEM is
 <file>:<line>:<severity>:<code>:<message>. --clear sends an empty set (clears any
 cached diagnostics from SOURCE). Use --instance to attach a view.json instance_path
 hint that consumers (the SPA's on-canvas badge layer in particular) use as a fast path
 instead of the file+line resolver.

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    source      TEXT        producer key (e.g. 'analysis-tool', 'claude-analysis'); │
│                              latest-writer-wins per source on the hub's cache        │
│                              [required]                                              │
│      items       [ITEMS]...  <file>:<line>:<sev>:<code>:<msg> ...                    │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --clear                 Send an empty items list (clears SOURCE).                    │
│ --instance        TEXT  Optional view.json instance_path to attach to every ITEM in  │
│                         this push. Use when the producer knows which instance a      │
│                         finding pertains to (most one-shot agent calls do); skip for │
│                         batch lint output where each item lives at a different       │
│                         file:line.                                                   │
│ --help                  Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
