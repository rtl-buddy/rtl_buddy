## Place and route

`rb pnr` takes the mapped netlist and SDC through OpenROAD floorplan, placement, clock tree, and routing, then reports timing, DRC, and electrical violations in one row. `rb pnr-export` streams the result to GDS and renders it with KLayout.

```bash
uv run rb pnr demo_tiny_alu_subsys_pnr_nangate45 -c pnr/demo_tiny_alu_subsys/pnr.yaml -l 1000
uv run rb pnr-export demo_tiny_alu_subsys_pnr_nangate45 -c pnr/demo_tiny_alu_subsys/pnr.yaml -l 1000 --png
```

```text
                                                      P&R Results Summary
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━━━━━┓
┃ P&R Run                          ┃ Result ┃ Description                    ┃ Cells ┃ Area        ┃ WNS Setup ┃ WNS Hold  ┃ DRCs ┃ Slew/Cap/Fanout ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━━━┩
│ demo_tiny_alu_subsys_pnr_nangat… │ PASS   │ P&R passed; pnr.no_wire_rc:    │ 3879  │ 8656.00 µm² │ +4.620 ns │ +0.030 ns │ 0    │ 1/50/0          │
│                                  │        │ pre-route steps saw no wire RC │       │             │           │           │      │                 │
│                                  │        │ (no layer-rc-tcl, EST-0018,    │       │             │           │           │      │                 │
│                                  │        │ CTS-0104); see rb docs show    │       │             │           │           │      │                 │
│                                  │        │ concepts/pnr#set-wire-rc;      │       │             │           │           │      │                 │
│                                  │        │ electrical 1 max-slew, 50      │       │             │           │           │      │                 │
│                                  │        │ max-capacitance violator(s)    │       │             │           │           │      │                 │
└──────────────────────────────────┴────────┴────────────────────────────────┴───────┴─────────────┴───────────┴───────────┴──────┴─────────────────┘
                                                   Results: 1 PASS (1 total)
```

![KLayout render of the routed demo_tiny_alu_subsys GDS on Nangate45](../static/img/tour/subsys-nangate45-routed.png)

The description column names the warnings the flow saw (here, pre-route steps estimated without wire RC) and points at the concept page that explains each one. The routed DEF, ODB, netlist, SDC, and reports stay in the run's artefact directory for the next step. See [Place-and-Route](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/).
