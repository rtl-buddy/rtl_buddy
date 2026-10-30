## wave-fpv

```text
Usage: rtl-buddy wave-fpv [OPTIONS] VERIF_NAME

 open SymbiYosys counterexample VCD for a failed FPV verification

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    verif_name      TEXT  name of FPV verification to open CEX for [required]       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --fpv-config  -c      TEXT  fpv.yaml to use [default: fpv.yaml]                      │
│ --surfer              TEXT  cfg-surfer entry name (default: the active platform's    │
│                             cfg-platforms surfer routing, else surfer-default)       │
│ --help                      Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
