## hub send phys-focus

```text
Usage: rtl-buddy hub send phys-focus [OPTIONS] TARGET

 Broadcast phys_focus{target} — point the hub's synth+power pane
 (http://127.0.0.1:<http_port>/phy) at one target of the run's physical model. TARGET
 is prefixed: 'instance:u_cpu/u_alu' or 'module:alu'; an unprefixed string is read as
 an instance path. --metric foregrounds one physical metric. The hub caches the focus
 and replays it to the pane on connect, so sending this before the browser tab is open
 works.

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    target      TEXT  physical target, e.g. module:alu or u_cpu/u_alu [required]    │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --metric        TEXT  cells|area|leakage|dynamic|total — which metric to foreground. │
│ --help                Show this message and exit.                                    │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
