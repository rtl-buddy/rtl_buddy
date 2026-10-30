## xplr diff

```text
Usage: rtl-buddy xplr diff [OPTIONS] EXP_A EXP_B

 pairwise experiment diff: knob delta, direction-aware outcome delta, and the git diff
 between the pinned sources

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    exp_a      TEXT  first experiment id [required]                                 │
│ *    exp_b      TEXT  second experiment id [required]                                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --patch          include the full git diff patch between the pinned sources (not     │
│                  just --stat)                                                        │
│ --help           Show this message and exit.                                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
