## Gate enable flops with clock-gating cells

By default every flop with a load enable maps to an enable flop that recirculates its output through a mux and is clocked every cycle. Production flows replace those with clock-gating cells, so an open-PDK run overstates their area and clock power. `clock-gating-min-flops` inserts integrated clock-gating cells (ICGs) with Yosys `clockgate`:

```yaml
cfg-synth-efforts:
  - name: gated
    yosys:
      clock-gating-min-flops: 8
```

- Set it in an effort, in `cfg-synth-tools` `opts`, or for one run in `tool_overrides.<tool>.clock_gating_min_flops`. Precedence is run override, then effort, then tool config. `0`, the default, is off; a run override of `0` turns off an effort's gating.
- Each set of at least that many rising-edge flops sharing a clock and enable gets one ICG, and its flops become plain flops. Falling-edge flops are left alone.
- The line runs after `synth` and `chformal -remove` and before `dfflibmap`, on both backends: `clockgate -liberty <cell libs> -dont_use <dont-use-cells> -min_net_size <n>`. `-liberty` picks the ICG from the corner's Liberty.
- When the Liberty's choice is wrong, name the cell and its enable, clock and gated-clock pins on the synth platform, for example `clock-gating-cell: 'sky130_fd_sc_hd__dlclkp_1 GATE:CLK:GCLK'`. The run then writes `clockgate -pos <cell> <pins> -min_net_size <n>`.
- It needs a Liberty: an unmapped `tool: yosys` run warns `synth.clock_gating_ignored` and gates nothing.
- The setting joins the run's options fingerprint only when on.
