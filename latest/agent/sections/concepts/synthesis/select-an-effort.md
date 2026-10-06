## Select an effort

Define reusable effort levels in `root_config.yaml`:

```yaml
cfg-synth-efforts:
  - name: quick
    yosys:
      synth-args: -flatten
      abc-script: "strash; dretime; map {D}"
    openroad:
      run: false

  - name: accurate
    openroad:
      run: true
      repair: true
      pre-sta-tcl: |
        set_wire_load_mode top
        set_wire_load_model -name Small
```

Select one with `effort: quick` in the run, or on the command line:

```bash
rb synth sandbox_openroad --effort quick
rb synth-regression --effort accurate
```

Precedence is the run's `tool_overrides`, then the selected effort, then `cfg-synth-tools`. With no effort, the built-in `standard` applies.

`openroad.run: false` skips OpenROAD and returns the Yosys result. `pre-sta-tcl` is raw Tcl run before STA; syntax errors appear only at runtime. The OpenROAD stage uses one thread unless the entry sets `threads:`; see [OpenROAD threads](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#openroad-threads).

`openroad.repair: true` repairs the netlist before the area and timing reports. OpenROAD sources the PDK's `layer-rc-tcl`, when it has one, right after `read_sdc`, so a `set_wire_rc` in `pre-sta-tcl` overrides it. After `pre-sta-tcl` and any `strategy` resynthesis it runs `repair_design` and, with an SDC, `repair_timing -setup`. The default is `false`. Repair changes only the reported area, WNS and TNS: `synth_netlist.v` is still Yosys' netlist, and `rb pnr` repairs it again after placement. The design is unplaced, so the repair sees pin loads but no wire lengths.
