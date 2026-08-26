## Configure tools and the PDK

Define backend defaults and map a named synthesis platform to a PDK corner in `root_config.yaml`:

```yaml
cfg-synth-tools:
  - name: yosys
    tool: yosys
    opts:
      synth-args: ""
      abc-args: ""
      frontend: verilog

  - name: openroad
    tool: openroad
    opts:
      strategy: AREA
      frontend: verilog

cfg-pdks:
  - name: sky130hd
    site: unithd
    corners:
      tt: pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
    tech-lef: pdk/sky130hd/lef/sky130_fd_sc_hd.tlef
    macro-lef: pdk/sky130hd/lef/sky130_fd_sc_hd_merged.lef

cfg-synth-platforms:
  - name: sky130hd_tt
    pdk: sky130hd
    corner: tt
```

All paths resolve from `root_config.yaml`.

The Yosys backend uses Liberty for mapping, area, and timing. The OpenROAD backend requires Liberty and technology/macro LEF; a missing LEF fails before running the tool. Keep large PDK files untracked and provide a reproducible fetch script.

OpenROAD `strategy` values are `AREA`, `TIMING`, `TIMING_ANNEAL`, and `TIMING_GENETIC`. `AREA` reports the initial mapping; the timing strategies request OpenROAD resynthesis.
