---
description: Install RTL Buddy with uv, verify it, and add the external tools required by each workflow.
---

# Installation

Install RTL Buddy in a Python 3.11 or newer project with [uv](https://docs.astral.sh/uv/).

## Install and verify

```bash
uv add rtl_buddy
uv run rb --version
```

The Python runtime dependencies install automatically. External EDA tools are needed only by the commands that use them.

## Dependency types

- **Required Python dependencies** come with the wheel.
- **Integrated tools** are fixed by a feature.
- **Pluggable tools** implement a supported interface.
- **Curated pluggable tools** are pluggable and also get tool-specific handling.

External tools stay optional until you run the workflow that uses them.

## External tools by feature

| Workflow | Required tools | Notes |
| --- | --- | --- |
| `test`, `randtest`, `regression` | A configured simulator: Verilator, Icarus Verilog, or VCS | Install `lcov` for Verilator LCOV/HTML export. |
| `elab`, `elab-regression` | `rtl_buddy[elab]` | `uv add "rtl_buddy[elab]"`. Supports pyslang 10.x and 11.x; needs no simulator. |
| Slurm dispatch | `sbatch`, `squeue`, `scancel`; `sacct` and `scontrol` recommended | Linux submit host and shared filesystem. See [Slurm dispatch tools](#slurm-dispatch-tools). |
| `verible` | Verible | macOS: `brew tap chipsalliance/verible && brew install verible`. |
| `synth`, `synth-regression` | [rtl-buddy Yosys fork](https://github.com/rtl-buddy/yosys); OpenROAD for `tool: openroad` | See [Synthesis](concepts/synthesis.md#install-the-tools). |
| `pnr`, `power` | OpenROAD 25Q1 or newer | KLayout is optional for P&R GDS and PNG output. |
| `phys` | None | Reads the `phys-model.json` and `phys-manifest.json` written by `rb synth` and `rb power`. See [Physical Metrics](concepts/phys.md). |
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

## Slurm dispatch tools

`--dispatch slurm` needs `sbatch`, `squeue`, and `scancel` on the submit host. Two more are recommended:

- `sacct` supplies the telemetry used for resource right-sizing.
- `scontrol` supplies `MaxArraySize` and `SchedulerParameters=max_array_tasks`. Without it, a group too large for one job array is not split. Set `cfg-dispatch.max-array-size`, and `cfg-dispatch.max-array-tasks` if the cluster caps tasks per array lower.

`scontrol` must also be on the `PATH` of the compute node that runs the build job. It lets each compile key's simulation jobs start as soon as that key is built; without it they wait for the whole build job.

Use `--dispatch local-parallel` for parallelism on one host with no Slurm dependency. See [Parallel dispatch](concepts/dispatch.md#meet-the-slurm-requirements).

## Update RTL Buddy

```bash
uv add rtl_buddy@latest
uv sync
```

Commit the changed `pyproject.toml` and lockfile.

## Install a pre-release

Pin a pre-release exactly, because version ranges do not select release candidates:

```bash
uv add "rtl_buddy==2.3.0rc1"
```

## Install the agent skills

Install the bundled Claude Code and Codex skill family at user scope:

```bash
uv run rb skill install
```

For a project pinned to a different RTL Buddy major version, install a project-local override:

```bash
uv run rb skill install --project
```

Re-run installation after each upgrade to refresh every family member. See [Agent Use](agents.md#bundled-agent-skills) for members, paths, status checks, and `.gitignore` handling.
