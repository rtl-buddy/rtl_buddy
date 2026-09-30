## External tools by feature

| Workflow | Required tools | Notes |
| --- | --- | --- |
| `test`, `randtest`, `regression` | A configured simulator: Verilator, Icarus Verilog, or VCS | Install `lcov` for Verilator LCOV/HTML export. |
| `elab`, `elab-regression` | `rtl_buddy[elab]` | `uv add "rtl_buddy[elab]"`. Supports pyslang 10.x and 11.x; needs no simulator. |
| Slurm dispatch | `sbatch`, `squeue`, `scancel`; `sacct` and `scontrol` recommended | Linux submit host and shared filesystem. See [Slurm dispatch tools](https://rtl-buddy.github.io/rtl_buddy/v6/install/#slurm-dispatch-tools). |
| `verible` | Verible | macOS: `brew tap chipsalliance/verible && brew install verible`. |
| `synth`, `synth-regression` | [rtl-buddy Yosys fork](https://github.com/rtl-buddy/yosys); OpenROAD for `tool: openroad` | See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/synthesis/#install-the-tools). |
| `pnr`, `power` | OpenROAD 25Q1 or newer | KLayout is optional for P&R GDS and PNG output. |
| `phys` | None | Reads the `phys-model.json` and `phys-manifest.json` written by `rb synth` and `rb power`. See [Physical Metrics](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/phys/). |
| `fpv`, `fpv-regression` | SymbiYosys 0.40 or newer and at least one SMT solver | Yosys is used for COI analysis; yosys-slang is optional. |
| `wave` | Surfer from the [rtl-buddy fork and branch](https://github.com/rtl-buddy/surfer/tree/rtl-buddy) | Mainline Surfer opens traces but has no live editor annotation. `rb nvim-install` also needs Git and network access. |
| `hier`, `hier-query` | `uv tool install rtl-buddy-sch` | Graphviz is optional for DOT rendering; pyslang for the slang frontend. |
| `graph build` | `rtl-buddy-sch`; optional `rtl_buddy[graph-extract]` | Query commands need only an existing graph. |
| `mcp` | `rtl_buddy[mcp]` | `uv add "rtl_buddy[mcp]"`. |
| `fpga` | Vivado, or Yosys + nextpnr-xilinx + prjxray for `tool: openxc7` | Missing optional FPGA tools produce `SKIP`. |
| `axi-profile` | `uv tool install rtl-buddy-axi-profiler` | Extras add Parquet and notebook support. |
| `mut` | `rtl_buddy[mut]` | `uv add "rtl_buddy[mut]"`. The selected oracle also needs its own tools. |
| Coverview packaging | Coverview and its `info-process` dependency | Basic coverage collection does not need Coverview. |

Run `rb tool-check` to diagnose missing or incompatible tools. Each command's concept page has setup and recovery details.
