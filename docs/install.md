# Installation

`rtl_buddy` is now published as a standalone open-source repo and is intended to be installed into your project environment with `uv`.

## Prerequisites

- Python 3.11 or later
- `uv`
- Simulation tool on `PATH`: Verilator (macOS/Linux) or VCS (Linux)
- Optional Verible binaries if you want to use `uv run rb verible ...`
- Optional system-level coverage tools:
  - `lcov` for `.info` export and HTML reports
  - Antmicro `coverview` for Coverview package generation

`rtl_buddy` can be used with different project-specific tool setups, but the primary supported flows are Verilator and VCS. Basic Verible command integration exists; broader first-class Verible and PeakRDL workflows are on the roadmap.

## Install Into A Project With `uv`

Add `rtl_buddy` to your project environment from the public Git repo:

```bash
uv add "rtl_buddy @ git+ssh://git@github.com/rtl-buddy/rtl_buddy.git@<tag-or-sha>"
```

If your environment cannot use SSH, use the HTTPS form instead:

```bash
uv add "rtl_buddy @ git+https://github.com/rtl-buddy/rtl_buddy.git@<tag-or-sha>"
```

Then verify the install:

```bash
uv run rb --version
```

## Updating

To move a project to a newer `rtl_buddy` version, update the pinned git ref in your project and resync the environment:

```bash
uv lock
uv sync
```

Commit the resulting lockfile change in your project repo.
