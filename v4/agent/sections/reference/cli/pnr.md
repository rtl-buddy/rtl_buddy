## pnr

```text
Usage: rtl-buddy pnr [OPTIONS] [PNR_NAME]                                              
                                                                                        
 run place-and-route                                                                    
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   pnr_name      [PNR_NAME]  name of pnr run                                          │
│                             [default: (run all entries in the suite)]                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --pnr-config  -c      TEXT     pnr.yaml to use [default: pnr.yaml]                   │
│ --list                         list pnr runs in the selected config and exit         │
│ --reg-level   -l      INTEGER  run only entries with reglvl at or below this value   │
│                                [default: 0]                                          │
│ --gds                          stream out GDS via KLayout after a successful P&R     │
│ --png                          render a PNG of the routed GDS via KLayout (implies   │
│                                --gds)                                                │
│ --help                         Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
