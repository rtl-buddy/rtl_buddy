## Terms

| Term | Meaning |
| --- | --- |
| PDK | Process design kit: one process's cell libraries, layer rules and layouts, named by a `cfg-pdks` entry. |
| Platform | A `cfg-pnr-platforms` entry: a PDK, its corners and P&R settings. |
| Corner | A process, voltage and temperature condition with its own Liberty timing, such as `ss`. The primary corner is the platform's first. |
| Liberty, LEF, DEF, ODB, GDS | Cell timing models; cell and layer outlines; a placed design as text; OpenROAD's database; the final layout. |
| SDC, SDC mode | The timing constraints file. A mode is one set of constraints that OpenSTA times on its own. |
| Slack, WNS, TNS | Slack is required minus arrival time; negative misses timing. WNS is the worst slack, TNS the sum of negative endpoint slacks. Setup checks the latest arrival, hold the earliest. |
| Slew, electrical violator | Slew is a signal edge's transition time. An electrical violator is a pin over its max-slew, max-capacitance or max-fanout limit. |
| DRC | Design rule check. A DRC violation is a routed shape that breaks a layer rule. |
| Parasitics, wire RC | Wire resistance and capacitance: estimated from placement or global route with a per-layer wire RC, or extracted from the detailed route into a SPEF file. |
| CTS | Clock-tree synthesis: building the buffered clock networks. |
| Global route, detailed route | Global route plans nets over a grid of GCells; overflow is demand above capacity. Detailed route draws the wires. |
| ECO | Engineering change order: rerouting only the nets a repair changed. |
| Legalization | Moving cells onto free, non-overlapping row sites after a step adds or moves them. `detailed_placement` does it. |
| Macro, RTL-MP | A block with a fixed layout, such as an SRAM or a hardened block. RTL-MP is OpenROAD's macro placer. |
| `repair_design`, `repair_timing` | OpenROAD's repairs: the first fixes slew, capacitance, fanout and long wires, the second setup or hold. |
| ORFS | OpenROAD-flow-scripts, the reference flow whose settings rb follows. |
| Stage, session | A stage ends at a checkpoint: `floorplan`, `place`, `cts`, `global_route`, `detailed_route`, `post_route_repair`, `finish`. A session is one OpenROAD process. |
| Abstract | The LEF, Liberty and GDS a hardened block publishes for a parent. |
