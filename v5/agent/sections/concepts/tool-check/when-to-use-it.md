## When to use it

- **First-time setup.** Right after `uv add rtl_buddy`, run `rb tool-check` to see which external tools you still need to install for the subcommands you care about.
- **CI gate.** A `rb tool-check --strict` step at the start of a CI job fails fast with an actionable error if the runner image drifted from the expected toolchain.
- **Triaging a "tool not found" error.** When a subcommand wrapper says "X not found — run `rb tool-check --explain X`", that's the canonical entry point.
- **After upgrading a tool.** Re-running `rb tool-check` after `brew upgrade verilator` (etc.) updates the cached version and re-evaluates `minimum_version` checks.
