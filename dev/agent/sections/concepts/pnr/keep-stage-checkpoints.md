## Keep stage checkpoints

A long routing run that hits a scheduler wall limit leaves nothing but its log: the flow writes its DEF and ODB only after detailed routing. Set `checkpoints:` on the run to keep a database at each stage boundary and a progress file that says where the run is:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    checkpoints: true            # or a stage, or a list: [cts, global_route]
```

| Stage | Written | Holds |
| --- | --- | --- |
| `floorplan` | before `global_placement` | floorplan, pins, tie cells, placed macros, PDN |
| `place` | before `clock_tree_synthesis` | legalized global placement after `repair_design` |
| `cts` | before `global_route` | clock tree, hold repair, legalization, `check_placement` |
| `global_route` | after a successful `global_route` | the global route: ODB, route guides and route segments |

Each stage writes `<NN>_<stage>.odb`, `.def` and `.sdc`; `global_route` also writes `.guide` (`write_guides`) and `.segments` (`write_global_route_segments`). `true` asks for all four stages and a list or a single name asks for some of them. An empty list (`checkpoints: []`) keeps the progress file and the manifest but writes no databases. Leave the key unset, or `false`, and the generated `pnr.tcl` is byte-for-byte what it was without the feature.

Checkpoints live in their own directory per run:

```
artefacts/<run>/checkpoints/
  latest -> 20260925T101500-4242
  20260925T101500-4242/
    manifest.json      # inputs, hashes, OpenROAD version, outcome
    progress.jsonl     # one JSON event per line, appended as the flow runs
    01_floorplan.odb  01_floorplan.def  01_floorplan.sdc
    ...
```

- **Progress.** `progress.jsonl` gets a `step_begin` and a `step_end` (`ok` or `error`, with the error text and the elapsed time) for every flow command — `global_placement`, `clock_tree_synthesis`, `global_route`, `detailed_route` and the rest — plus a `checkpoint` event once all of a stage's files are on disk. Each line is flushed as it is written, so `tail -f artefacts/<run>/checkpoints/latest/progress.jsonl` follows a running flow, and after a kill the last `step_begin` with no `step_end` is the step the run was in. The Tcl side only appends events; RTL Buddy writes `run_start` and `run_end` around it.
- **Manifest.** `manifest.json` is written before OpenROAD starts, with SHA-256 fingerprints of the netlist, the SDC, every Liberty and LEF, the pin-constraints and PDN snippets, and the generated `pnr.tcl`, and the OpenROAD path and version. After OpenROAD exits it gains the outcome, the step the run stopped in, and a fingerprint of every checkpoint file.
- **Never final.** A checkpoint is never named `*.routed.*` and never sits where `rb power` or a plain `rb pnr-export` looks, and every manifest entry says `final: false`, `detail_routed: false`, and whether it is `global_routed`. No checkpoint carries a congestion grid — before global routing there is none, and the flow's `global_route` writes no congestion report — so each entry says `congestion.available: false` with the reason, rather than anything a reader could take for zero congestion.
- **Survives failure; never reused.** The routed outputs keep their own rules (see [Inspect artefacts](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#inspect-artefacts)): a failed run removes them. Checkpoints are the opposite — they exist to outlive a failure — so a run cannot clear them; instead each run writes into a new `<timestamp>-<pid>` directory and a later run never overwrites or deletes an earlier one's. `latest` is what marks the current run's: every `rb pnr` run removes it first thing, checkpointed or not, and a checkpointed run points it at its own directory once OpenROAD is launched. Older run directories are kept until you delete them, and they are not pruned automatically — each holds a few databases, so clear out the ones you no longer need.

Resume is not implemented. The manifest is shaped for it: a resume must re-read the libraries, the ODB and SDC, and reapply the routing-layer and wire-RC settings, and it should refuse a checkpoint whose recorded input or OpenROAD fingerprints no longer match. A resume from `global_route` needs the `.segments` file as well as the `.guide`: `read_guides` restores the guides but, as it warns, not the parasitics a global-route estimate is made from — only `read_global_route_segments` brings those back. Those are global-route estimates either way, not extracted detailed-route RC.

To look at a checkpoint, open its ODB in OpenROAD, or stream its DEF out with [`rb pnr-export --checkpoint`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#export-a-saved-result).
