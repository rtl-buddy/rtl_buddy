## hub send wave-move

```text
Usage: rtl-buddy hub send wave-move [OPTIONS] IDS...

 Reorder items in surfer's view. Move the given IDS (in the order listed) so the block
 starts at --to INDEX, or just before --before ID. Exactly one of --to / --before is
 required.

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    ids      IDS...  DisplayedItemRef ids to move, e.g. 5 6 [required]              │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --to            INTEGER RANGE [x>=0]  target visible index (0 = top of view)         │
│ --before        INTEGER RANGE [x>=0]  move the block to just before this item id     │
│                                       (resolved via wave-items)                      │
│ --help                                Show this message and exit.                    │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
