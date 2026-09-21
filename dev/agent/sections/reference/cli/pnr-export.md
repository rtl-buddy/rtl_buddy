## pnr-export

```text
Usage: rtl-buddy pnr-export [OPTIONS] [PNR_NAME]

 export GDS/PNG from a saved P&R result (no synthesis, no OpenROAD)

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   pnr_name      [PNR_NAME]  name of pnr run whose saved result to export             │
│                             [default: (export every entry in the suite)]             │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --pnr-config  -c      TEXT              pnr.yaml to use [default: pnr.yaml]          │
│ --list                                  list pnr runs in the selected config and     │
│                                         exit                                         │
│ --reg-level   -l      INTEGER           export only entries with reglvl at or below  │
│                                         this value                                   │
│                                         [default: 0]                                 │
│ --png                                   render a PNG of the exported GDS             │
│ --png-only                              re-render the PNG from the GDS already in    │
│                                         the artefact directory; no stream-out, and   │
│                                         the GDS is not rewritten                     │
│ --def                 TEXT              export this DEF instead of the run's own     │
│                                         routed one; needs a single named run, whose  │
│                                         platform and top are used                    │
│                                         [default: (the run's <top>.def)]             │
│ --lyp                 TEXT              layer properties (.lyp) for the render       │
│                                         [default: (the PDK's klayout-props)]         │
│ --png-width           INTEGER           rendered PNG width in pixels [default: 2048] │
│ --png-height          INTEGER           rendered PNG height in pixels                │
│                                         [default: 2048]                              │
│ --gds-mode            [strict|preview]  override each run's gds-mode: strict         │
│                                         publishes nothing when a cell has no layout, │
│                                         preview keeps the incomplete layout and      │
│                                         reports the cells                            │
│                                         [default: (each run's gds-mode (preview))]   │
│ --help                                  Show this message and exit.                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
