## Quick start

```bash
# Default — text report of all tools + per-subcommand readiness
rb tool-check

# JSON for scripting / CI
rb tool-check --format json

# Only the deps relevant to one subcommand
rb tool-check --required-for fpv

# Install instructions for a single tool
rb tool-check --explain surfer

# Fail the shell when something required is missing/outdated
rb tool-check --strict
```

`rb tool-check` runs at the top level — it does not require a `root_config.yaml`, a suite directory, or any prior command. The `--include-optional/--no-include-optional` flag (default on) controls whether optional tools (gtkwave, klayout, graphviz, pyslang, cocotb, FPV solvers, etc.) appear in the report.
