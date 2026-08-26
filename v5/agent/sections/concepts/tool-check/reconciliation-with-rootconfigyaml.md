## Reconciliation with `root_config.yaml`

When a project's `root_config.yaml` is discoverable from the current directory, `rb tool-check` reconciles it with the manifest:

- **`cfg-verible`** — the active platform's verible directory is added to verible's detector chain as the *preferred* lookup, with `PATH` retained as fallback.
- **`cfg-surfer`** — the `surfer-default` entry's resolved path is added similarly.
- **`cfg-tools`** — overrides `minimum_version` for any matching tool. Project pins always win over manifest defaults.
- **`cfg-fpv-tools[*].opts.solver-versions`** — pins each FPV solver to an exact version. Runtime semantics is exact-equality (`rb fpv` hard-fails on mismatch); `rb tool-check` surfaces the pin as `minimum_version` so users see a single "outdated" indication for solvers that don't match.

Outside a project (no `root_config.yaml` discoverable), the manifest defaults apply unchanged. The "outside a project" mode is important for first-run setup: `rb tool-check` after `pip install rtl_buddy` is a valid invocation and tells the user what to install before they create a project.
