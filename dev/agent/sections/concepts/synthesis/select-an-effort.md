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

  - name: standard
    openroad:
      run: true

  - name: accurate
    openroad:
      run: true
      pre-sta-tcl: |
        set_wire_load_mode top
        set_wire_load_model -name Small
```

Select an effort in the run or on the CLI:

```yaml
effort: quick
```

```bash
rb synth sandbox_openroad --effort quick
rb synth-regression --effort accurate
```

Precedence is per-run `tool_overrides`, then the selected effort, then `cfg-synth-tools`. Without a configured or selected effort, RTL Buddy uses built-in `standard` behavior.

`openroad.run: false` skips OpenROAD and returns the Yosys result. `pre-sta-tcl` is raw Tcl executed before STA; test it on a small design because syntax and tool errors appear only at runtime.

The OpenROAD stage runs on one thread unless the synthesis entry sets `threads:` — a positive integer, or `auto` for the CPUs of the current allocation. It matters most for a `pre-sta-tcl` that runs global placement. The value is validated, clamped, emitted and recorded exactly as for P&R; see [OpenROAD threads](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#openroad-threads). It has no effect on the Yosys stage.
