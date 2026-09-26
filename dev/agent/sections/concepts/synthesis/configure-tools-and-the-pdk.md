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

Synthesis reads only a PDK's Liberty corner, LEFs and `dont-use-cells`, so the per-PDK notes are short:

- **Nangate45** — one Liberty file (`NangateOpenCellLibrary_typical.lib`); the project template's `synth/demo_tiny_alu_subsys/download_pdk.sh` fetches it.
- **sky130hd** — one Liberty per corner (`sky130_fd_sc_hd__tt_025C_1v80.lib`), plus the `dont-use-cells` list for the probe and `lpflow` cells; see the template's `sky130hd` entry and `synth/demo_tiny_alu_subsys_hier/download_pdk.sh`.
- **ASAP7** — not validated by rtl_buddy. Its cells are split across one Liberty file per cell group per Vt and corner, mostly gzipped, and `corners:` takes one file per corner, so merge a corner's files into one Liberty first, as ORFS does for its synthesis step.

The P&R side of each PDK, including the ASAP7 gaps, is in [Place-and-Route: PDK setup notes](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#pdk-setup-notes).

A PDK's `dont-use-cells` list excludes cells from mapping: each pattern becomes a `-dont_use` argument to Yosys `dfflibmap` and `abc`, and on the OpenROAD backend a `set_dont_use` before the resynthesis stage reads the netlist. It is the same list [P&R](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#tune-the-process-dependent-steps) reads, so a cell excluded here is excluded there too, and two runs that exclude different cells fingerprint as two experiments. A synth platform's own `dont-use-cells` is added to the PDK's list, PDK entries first, as on a P&R platform.
