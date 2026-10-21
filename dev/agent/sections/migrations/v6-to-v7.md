## v6 to v7

### Machine output and reservation keys

Machine-mode stderr summaries omit `PASS`, `SKIP`, and `XFAIL` rows but retain the full tally. Read stdout results or `summary.rows` for every row. Human mode still shows all rows unless `--print-failures-only` is set. Per-phase status lines require `--verbose` or `--debug`; dispatch progress, job announcements, hooks, warnings, and errors remain visible.

Update consumers of `payload.reservation_advice`:

- Stop reading `event`; findings omit it.
- Treat missing `reg_level`, `allocated`, `suggested_total`, and `aggregate_delta` as null. Regression levels remain per finding, absent on build-job advice.
- Keep applying the explicit `edit_hint`; names, units, ordering, and provenance are unchanged. See [Apply reservation advice](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#apply-reservation-advice) for the payload contract.

`rightsize.advice` moves from INFO to DEBUG. Use `--debug` for individual events or read the complete machine payload. A nonempty list logs INFO `rightsize.summary` with `total` and direction `counts` (`raise`, `reduce`).

Unknown reservation and dispatch keys now stop loading with exit code 2. Correct or remove every reported key; the [YAML reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#parallel-dispatch) lists checked blocks. Mode names remain free-form; mode contents remain strict.

Log consumers must replace WARNING `config.unknown_key` with ERROR `config.unknown_key_rejected`. Diagnostics include the file, block, key, nearest spelling, and valid keys. Nested locations use `cfg-dispatch: compile.verilate` instead of `cfg-dispatch.compile.verilate`.

### Unknown P&R keys are errors

An unknown key in a P&R run, a P&R platform or a PDK is a config error. Before, it was dropped without a word, so a misspelt key changed the flow silently: a blockage written with `lifetme: global-placement` loaded as a permanent one.

The check covers:

- Every mapping of a `pnr.yaml` run: the run, `floorplan`, `floorplan.blockages[]`, `floorplan.pins[]`, `floorplan.macros[]`, `pdn`, `pdn.ring`, `pdn.stripes[]`, `abstract`, `placement` and `blocks[]`.
- Every `cfg-pnr-platforms` entry, with its `routing-layers` and `placement` blocks.
- Every `cfg-pdks` entry, with its `pin-layers` and `placement` blocks.

Each unknown key is reported with the file, its location, the nearest valid key and every valid key:

```text
pnr.yaml: pnr run 'top': floorplan.blockages[2]: unknown key 'lifetme', did you mean 'lifetime'? (valid keys: rect, type, max-density, lifetime)
```

Remove the key or correct its spelling. A `pnr.yaml` error stops `rb pnr`. A `cfg-pdks` or `cfg-pnr-platforms` error stops every command that loads `root_config.yaml`. See [YAML formats](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#pnryaml).

### The routed DEF must keep located macros and exact pins

A run with a located macro or an exact pin compares them with the routed DEF and fails with `fail_stage: routed_check` when one moved; `xfail:` does not excuse it. In v6 nothing compared them, so a run whose `pin-constraints` file or platform hook moved a located macro or an exact pin passed. Remove the conflicting placement or the floorplan entry. See [Place IO pins](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#place-io-pins).

### A flow that ends early is a failure

A run whose OpenROAD session exits 0 before its flow's end marker fails with `fail_stage: incomplete`; `xfail:` does not excuse it. In v6 a run was judged on the exit code and the log's `[ERROR` lines, so a run whose `platform-tcl`, `layer-rc-tcl`, `tracks-tcl` or `tapcell-tcl` file, `pin-constraints` file or `pdn-config` called `exit` passed with missing metrics and outputs. Remove the `exit`, or the `return -level 2`, from the file that the step named in the message sources. See [Interpret results](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#interpret-results).

### Supply pins of added cells are connected

The flow applies the PDK's global connections again (`global_connect`) at the end of every stage, before detailed routing, after fill and before the final outputs. In v6 only the cells present when `pdngen` ran had their supply pins connected; the buffers, tie cells and fill added later kept them unconnected in the routed ODB, DEF and every checkpoint. The generated `pnr.tcl` of every run gains the call, and the routed DEF of a run with a `pdn-config` lists the added cells in its power and ground nets. Detailed routing now starts from a design with connected supply pins, so a run's route and slack can differ slightly. No `pnr.yaml` change is needed. See [Plan the power grid](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#plan-the-power-grid).

### Exact pins leave side and group rules

A port with an exact location in `floorplan.pins` is left out of every side or group rule that also matches it. In v6 it stayed in the rule's list: the run failed with `PPL-0107` when the pin lay outside the rule's edge, and otherwise the rule's other pins were placed around it. Such a config now places the rule's other pins differently.

### The routed netlist declares no supply ports

`<top>.routed.v` is the logical netlist in every run: it declares no supply ports and connects no cell supply pin. In v6 it declared the supply nets the power grid created, such as `VDD` and `VSS`, as `inout` ports that nothing inside the module connected to. A hardened block's `<top>.lib` lists those supply pins with `direction : input`, where v6 wrote `inout`. A script or testbench that connects the supply ports of a routed netlist by name must drop those connections. See [Inspect artefacts](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#inspect-artefacts).

### Stage checkpoints

The `global_route` checkpoint holds the design after the `after-global-route` hooks and the global-route repair (`global-route-hold-repair` or `global-route-repair`), before the `before-detailed-route` hooks: the design detailed routing starts from. In v6 it held the design as `global_route` returned, before the hold repair. A `detailed_route` checkpoint (`05_detailed_route.odb`, `.def` and `.sdc`) follows detailed routing, so `checkpoints: true` writes five stages and `rb pnr-export --checkpoint detailed_route` exports the route before fill. Checkpoint SDC files are written with `write_sdc -no_timestamp -digits 9`. No `pnr.yaml` change is needed; a script that reads `04_global_route` as the design before hold repair gets the repaired design. See [Keep stage checkpoints](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints).

A one-session run that reaches its end without writing a checkpoint it keeps fails with `fail_stage: checkpoint`. In v6 it passed without that checkpoint, which happened when a checkpoint's write failed or when a hook wrapped the stage's boundary command (`global_placement`, `clock_tree_synthesis`, `global_route`) with `rename`. Fix the write's cause or remove the wrapper, or set `stages: true`.

### OpenROAD runs in the artefact directory

Every OpenROAD session runs in the run's artefact directory, `artefacts/<run>/`. In v6 it ran in the directory `rb` was started from, so a relative path in a run hook, `platform-tcl`, `layer-rc-tcl`, `tapcell-tcl`, `pin-constraints` file or `pdn-config` (`source`, `open`, `read_*`, `write_*`) resolved there. Such a path now resolves from `artefacts/<run>/`. Build paths from the script's own location instead, `[file join [file dirname [info script]] <name>]`, or from the variables the flow sets (`OUT_DIR`). See [Run Tcl hooks](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-tcl-hooks).

### A multi-corner platform times each corner in its own SDC mode

On a platform with `corners:`, `rb pnr` times each corner in an OpenSTA SDC mode and scene of its own, and the final reports read the routed SPEF once per corner. In v6 the corners shared one mode and one SPEF read, so a generated clock's latency came from one corner and the later corners reused the first corner's parasitics. Multi-corner slack and repairs change; single-corner runs do not.

- An SDC command in a hook constrains the primary corner only. Put it in the SDC, or run it after `set_mode <corner>` for each corner, then `set_mode <primary>`.
- `max_fanout_violation_count` counts a pin once per corner it violates at.
- Each corner also gets `<top>.<corner>.routed.sdc` and, in checkpoints, `<NN>_<stage>.<corner>.sdc`. `<top>.routed.sdc` holds the primary corner's constraints, and a checkpoint's `.sdc` every mode.
- A `--resume` of a multi-corner run started by v6 is refused; rerun it from the start.
- OpenROAD 26Q2 keeps one shared mode and warns `pnr.corner_modes_unsupported`. An OpenROAD that does not answer rb's mode check fails setup.

See [Sign off at several corners](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#sign-off-at-several-corners).

### Synthesis repair buffers the unplaced netlist

A synthesis effort with `openroad.repair: true` runs `repair_design -pre_placement` when no instance is placed after its `pre-sta-tcl`. In v6 it ran plain `repair_design`, which on an unplaced netlist only resizes, so high-fanout nets stayed unbuffered. Such runs usually report more area and less negative WNS and TNS, and their options digest changes. No config change is needed. See [Select an effort](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/synthesis/#select-an-effort).
