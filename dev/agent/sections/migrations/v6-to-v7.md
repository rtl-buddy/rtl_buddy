## v6 to v7

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

A run with a located macro or an exact pin compares them with the routed DEF and fails with `fail_stage: routed_check` when one moved; `xfail:` does not excuse it. In v6 nothing compared them, so a run whose `pin-constraints` file or platform hook moved a located macro or an exact pin passed. Remove the conflicting placement or the floorplan entry. See [Check the routed floorplan](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#check-the-routed-floorplan).

### Exact pins leave side and group rules

A port with an exact location in `floorplan.pins` is left out of every side or group rule that also matches it. In v6 it stayed in the rule's list: the run failed with `PPL-0107` when the pin lay outside the rule's edge, and otherwise the rule's other pins were placed around it. Such a config now places the rule's other pins differently.

### Stage checkpoints

The `global_route` checkpoint holds the design after the `after-global-route` hooks and the global-route repair (`global-route-hold-repair` or `global-route-repair`), before the `before-detailed-route` hooks: the design detailed routing starts from. In v6 it held the design as `global_route` returned, before the hold repair. A `detailed_route` checkpoint (`05_detailed_route.odb`, `.def` and `.sdc`) follows detailed routing, so `checkpoints: true` writes five stages and `rb pnr-export --checkpoint detailed_route` exports the route before fill. Checkpoint SDC files are written with `write_sdc -no_timestamp -digits 9`. No `pnr.yaml` change is needed; a script that reads `04_global_route` as the design before hold repair gets the repaired design. See [Keep stage checkpoints](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints).

A one-session run that reaches its end without writing a checkpoint it keeps fails with `fail_stage: checkpoint`. In v6 it passed without that checkpoint, which happened when a checkpoint's write failed or when a hook wrapped the stage's boundary command (`global_placement`, `clock_tree_synthesis`, `global_route`) with `rename`. Fix the write's cause or remove the wrapper, or set `stages: true`.

### OpenROAD runs in the artefact directory

Every OpenROAD session runs in the run's artefact directory, `artefacts/<run>/`. In v6 it ran in the directory `rb` was started from, so a relative path in a run hook, `platform-tcl`, `layer-rc-tcl`, `tapcell-tcl`, `pin-constraints` file or `pdn-config` (`source`, `open`, `read_*`, `write_*`) resolved there. Such a path now resolves from `artefacts/<run>/`. Build paths from the script's own location instead, `[file join [file dirname [info script]] <name>]`, or from the variables the flow sets (`OUT_DIR`). See [Run Tcl hooks](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-tcl-hooks).
