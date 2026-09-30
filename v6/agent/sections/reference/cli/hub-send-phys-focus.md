## hub send phys-focus

```text
Usage: rtl-buddy hub send phys-focus [OPTIONS] TARGET

 Broadcast phys_focus{target}: point the synth and power pane (/phy) at one target.
 TARGET is 'instance:u_cpu/u_alu' or 'module:alu'; an unprefixed string is an instance
 path. --metric foregrounds one physical metric. The graph pane (/gph) also follows: it
 turns on its heat overlay and highlights the target's module. The hub replays the
 focus when either pane connects, so it can be sent before the tabs are open.

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    target      TEXT  physical target, e.g. module:alu or u_cpu/u_alu [required]    │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --metric        TEXT  cells|area|leakage|dynamic|total — which metric to foreground. │
│ --help                Show this message and exit.                                    │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
