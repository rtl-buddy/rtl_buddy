## pnr

```text
Usage: rtl-buddy pnr [OPTIONS] [PNR_NAME]

 run place-and-route

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   pnr_name      [PNR_NAME]  name of pnr run                                          │
│                             [default: (run all entries in the suite, each block      │
│                             before the runs that consume it)]                        │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --pnr-config    -c      TEXT                  pnr.yaml to use [default: pnr.yaml]    │
│ --list                                        list pnr runs in the selected config   │
│                                               and exit                               │
│ --reg-level     -l      INTEGER               run only entries with reglvl at or     │
│                                               below this value                       │
│                                               [default: 0]                           │
│ --gds                                         stream out GDS via KLayout after a     │
│                                               successful P&R                         │
│ --png                                         render a PNG of the routed GDS via     │
│                                               KLayout (implies --gds)                │
│ --gds-mode              [strict|preview]      override each run's gds-mode (implies  │
│                                               --gds): strict fails the run when a    │
│                                               cell has no layout, preview keeps the  │
│                                               incomplete layout and reports the      │
│                                               cells                                  │
│                                               [default: (each run's gds-mode         │
│                                               (preview))]                            │
│ --accept-stale                                consume blocks: abstracts whose        │
│                                               recorded inputs changed since they     │
│                                               were hardened, qualifying the result   │
│                                               instead of failing                     │
│ --jobs          -j      INTEGER RANGE [x>=1]  P&R runs at once: independent blocks   │
│                                               harden side by side, and a top waits   │
│                                               for all of its own. Each run is a full │
│                                               OpenROAD session sized by its          │
│                                               `threads:` setting, so size the two    │
│                                               together                               │
│                                               [default: 1]                           │
│ --synth                                       run each P&R run's upstream synthesis  │
│                                               just before it, once per synthesis —   │
│                                               so a top built from blocks: is         │
│                                               synthesized after its blocks are       │
│                                               hardened                               │
│ --help                                        Show this message and exit.            │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
