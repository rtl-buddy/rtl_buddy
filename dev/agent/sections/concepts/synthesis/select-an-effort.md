## Select an effort

Define reusable effort levels in `root_config.yaml`:

```yaml
cfg-synth-efforts:
  - name: quick
    yosys:
      synth-args: -flatten
      abc-args: -fast
    openroad:
      run: false

  - name: accurate
    openroad:
      run: true
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

`openroad.run: false` skips OpenROAD and returns the Yosys result. `pre-sta-tcl` is raw Tcl run before STA; syntax errors appear only at runtime. The OpenROAD stage uses one thread unless the entry sets `threads:`; see [OpenROAD threads](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#openroad-threads).
