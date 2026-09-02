## External tools by feature

| Workflow | Required tools | Notes |
| --- | --- | --- |
| `test`, `randtest`, `regression` | A configured simulator: Verilator, Icarus Verilog, or VCS | Install `lcov` for Verilator LCOV/HTML export. |
| `elab`, `elab-regression` | `rtl_buddy[elab]` | Install with `uv add "rtl_buddy[elab]"`; supports pyslang 10.x and 11.x and needs no simulator executable. |
| Slurm dispatch | `sbatch`, `squeue`, `scancel`; `sacct` and `scontrol` recommended | Requires a Linux submit host and shared filesystem. `sacct` supplies right-sizing telemetry; `scontrol` supplies `MaxArraySize`, without which a resource group too large for one job array is not split — set `cfg-dispatch.max-array-size` instead. Use `--dispatch local-parallel` for dependency-free parallelism on one host. |
| `verible` | Verible | macOS: `brew tap chipsalliance/verible && brew install verible`. |
| `synth`, `synth-regression` | [rtl-buddy Yosys fork](https://github.com/rtl-buddy/yosys); OpenROAD for `tool: openroad` | See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/synthesis/#install-the-tools). |
| `pnr`, `power` | OpenROAD 25Q1 or newer | KLayout is optional for P&R GDS and PNG output. |
| `fpv`, `fpv-regression` | SymbiYosys 0.40 or newer and at least one SMT solver | Yosys is used for COI analysis; yosys-slang is optional. |
| `wave` | Surfer from the [rtl-buddy fork and branch](https://github.com/rtl-buddy/surfer/tree/rtl-buddy) | Mainline Surfer opens traces but lacks live editor annotation. `rb nvim-install` additionally needs Git and network access. |
| `hier`, `hier-query` | `uv tool install rtl-buddy-sch` | Graphviz is optional for DOT rendering; pyslang is optional for the slang frontend. |
| `graph build` | `rtl-buddy-sch`; optional `rtl_buddy[graph-extract]` | Query commands need only an existing graph. |
| `mcp` | `rtl_buddy[mcp]` | Install with `uv add "rtl_buddy[mcp]"`. |
| `fpga` | Vivado, or Yosys + nextpnr-xilinx + prjxray for `tool: openxc7` | Missing optional FPGA tools produce `SKIP`. |
| `axi-profile` | `uv tool install rtl-buddy-axi-profiler` | Extras provide Parquet and notebook support. |
| `mut` | `rtl_buddy[mut]` | Install with `uv add "rtl_buddy[mut]"`; the selected oracle also needs its normal tools. |
| Coverview packaging | Coverview and its `info-process` dependency | Basic coverage collection does not require Coverview. |

Use `rb tool-check` where supported to diagnose missing or incompatible tools. The linked concept page for each command contains setup and recovery details.
