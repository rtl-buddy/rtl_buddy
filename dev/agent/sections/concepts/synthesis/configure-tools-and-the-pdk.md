## Configure tools and the PDK

`root_config.yaml` holds backend defaults and maps a named synthesis platform to a PDK corner:

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

Paths resolve from `root_config.yaml`. The Yosys backend needs Liberty. The OpenROAD backend needs Liberty plus technology and macro LEF. Keep large PDK files untracked and provide a fetch script.

OpenROAD `strategy` is `AREA`, `TIMING`, `TIMING_ANNEAL`, or `TIMING_GENETIC`. `AREA` reports the initial mapping; the timing strategies request OpenROAD resynthesis.

Synthesis reads only a PDK's Liberty corner, LEFs and `dont-use-cells`:

- **Nangate45**: one Liberty file. The template's `synth/demo_tiny_alu_subsys/download_pdk.sh` fetches it.
- **sky130hd**: one Liberty per corner, plus a `dont-use-cells` list for the probe and `lpflow` cells. See the template's `sky130hd` entry.
- **ASAP7**: not validated. `corners:` takes one file per corner, so merge ASAP7's split gzipped Liberty files first.

`dont-use-cells` patterns exclude cells from mapping. A synth platform's own list is appended to the PDK's. [Place-and-Route](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#tune-the-process-dependent-steps) reads the PDK's list too, so two runs that exclude different cells count as two experiments. P&R-side PDK notes are in [Place-and-Route: PDK setup notes](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#pdk-setup-notes).
