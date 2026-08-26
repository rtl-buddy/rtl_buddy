## Prerequisites

- Python 3.11 or later
- `uv`
- Simulation tool on `PATH`: Verilator (macOS/Linux) or VCS (Linux)
- Optional Verible binaries if you want to use `uv run rb verible ...`
- Optional system-level coverage tools:
  - `lcov` for `.info` export and HTML reports
  - Antmicro `coverview` for Coverview package generation

`rtl_buddy` can be used with different project-specific tool setups, but the primary supported flows are Verilator and VCS. Basic Verible command integration exists; broader first-class Verible and PeakRDL workflows are on the roadmap.
