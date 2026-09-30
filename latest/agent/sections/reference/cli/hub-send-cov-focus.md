## hub send cov-focus

```text
Usage: rtl-buddy hub send cov-focus [OPTIONS] TARGET

 Broadcast cov_focus{target}: point the coverage pane (/cov) at one target. TARGET is
 'file:design/blk.sv', 'module:blk' or 'test:verif/blk#basic'; an unprefixed string is
 a file path. --metric foregrounds one coverage kind, --line scrolls a file target to a
 line, and --item names a bin or SVA cover point. The hub replays the focus when the
 pane connects, so it can be sent before the tab is open.

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    target      TEXT  coverage target, e.g. module:blk or design/blk.sv [required]  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --metric        TEXT                  line|branch|toggle|expression|cover — which    │
│                                       kind to foreground.                            │
│ --line          INTEGER RANGE [x>=1]  1-based source line to scroll to.              │
│ --item          TEXT                  Point within the target: a                     │
│                                       branch/toggle/expression bin name as /cov.json │
│                                       spells it, or an SVA cover point name.         │
│ --help                                Show this message and exit.                    │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
