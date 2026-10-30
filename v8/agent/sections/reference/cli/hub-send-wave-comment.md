## hub send wave-comment

```text
Usage: rtl-buddy hub send wave-comment [OPTIONS] TEXTS...

 Add comment rows (named dividers) to surfer's view. Returns the new item ids. Maps to
 WCP add_dividers.

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    texts      TEXTS...  comment labels, one divider per entry [required]           │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --after        INTEGER RANGE [x>=0]  insert the comments after this item id          │
│                                      (default: end of view)                          │
│ --help                               Show this message and exit.                     │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
