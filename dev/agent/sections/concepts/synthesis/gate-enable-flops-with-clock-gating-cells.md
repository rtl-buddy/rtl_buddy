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
- A flop whose synchronous reset has priority over its enable, as `if (rst) q <= 0; else if (en) q <= d;` infers, is gated on its enable or its reset, so its gated clock also ticks while the reset is asserted and the reset still takes effect. Flops that share an enable but not a reset therefore get separate ICGs.
- The lines run after `synth` and `chformal -remove` and before `dfflibmap`, on both backends. `dfflegalize -cell $_SDFFCE_????_ 01 t:$_SDFFE_*` gives each reset-over-enable flop the enable `en | rst` and makes it an enable-over-reset flop, `opt_merge` and `opt_clean` merge the new enables, then `clockgate -liberty <cell libs> -dont_use <dont-use-cells> -min_net_size <n>` gates. `-liberty` picks the ICG from the corner's Liberty.
- When the Liberty's choice is wrong, name the cell and its enable, clock and gated-clock pins on the synth platform, for example `clock-gating-cell: 'sky130_fd_sc_hd__dlclkp_1 GATE:CLK:GCLK'`. The run then writes `clockgate -pos <cell> <pins> -min_net_size <n>`.
- It needs a Liberty: an unmapped `tool: yosys` run warns `synth.clock_gating_ignored` and gates nothing.
- The setting joins the run's options fingerprint only when on.

`clock-gating-max-flops` bounds the other side, as commercial flows do (8 to 16 flops is typical). It is set where `clock-gating-min-flops` is, `0` (the default) is no limit, and it must not be below `clock-gating-min-flops`:

- A set of n flops above the limit gets ceil(n/max) ICGs: the one `clockgate` inserted and copies of it on the same clock, enable and tie inputs. The flops are dealt in cell-name order in near-equal shares.
- After `clockgate`, the Yosys script writes the design to `yosys-tmp/clockgate.il` with `write_rtlil`, rtl_buddy's `icg_split` helper rewrites it to `yosys-tmp/clockgate_split.il` and removes the input, and `read_rtlil` loads the result in place of the old design. The helper changes only the lines of a split, so every other cell, including its parameters, comes back unchanged. `synth.log` has one `icg_split:` line per split ICG; mapping, `stat` and the netlist include the copies.
- An ICG whose cell has more than one output bit cannot be split. When it drives more flops than the limit, the run warns `synth.icg_not_split`.
- It applies only to ICGs `clockgate` inserted, not to clock-gating cells instantiated in the RTL.
- Without `clock-gating-min-flops`, nothing is gated, so the limit is ignored and the run warns `synth.clock_gating_max_ignored`.
