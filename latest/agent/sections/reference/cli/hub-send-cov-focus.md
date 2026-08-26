## hub send cov-focus

```text
Usage: rtl-buddy hub send cov-focus [OPTIONS] TARGET

 Broadcast cov_focus{target} — point the hub's coverage pane
 (http://127.0.0.1:<http_port>/cov) at one target of the run's coverage model. TARGET
 is prefixed: 'file:design/blk.sv', 'module:blk', or 'test:verif/blk#basic'; an
 unprefixed string is read as a file path. --metric foregrounds one coverage kind,
 --line scrolls a file target to a line, and --item names a branch/toggle/expression
 bin or an SVA cover point. The hub caches the focus and replays it to the pane on
 connect, so sending this before the browser tab is open works.

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
