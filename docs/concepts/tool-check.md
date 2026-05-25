---
description: How to verify the rtl_buddy external-tool environment with the rb tool-check command, the declarative tool manifest, and the per-subcommand readiness report.
---

# Tool Dependency Check

`rb tool-check` reports which external tools `rtl_buddy` has located, which subcommands are ready to run, and which are blocked on a missing or outdated dependency. It is a diagnostic surface for the declarative [tool manifest](#how-the-manifest-works) — the same source of truth that subcommand wrappers consult when they refuse to run because a tool is missing.

`rb tool-check` must work both inside and outside a project: it opportunistically discovers a `root_config.yaml` to honor project pins (verible/surfer paths, `cfg-tools` min-versions, `cfg-fpv-tools` solver pins), but degrades gracefully when none is present.

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

## Output

The default text format has two sections plus a hint line:

```
Tools (12 ok, 1 missing, 1 outdated)
----------------------------------------------------------------------
Tool                  Status      Version       Path
verible               ok          v0.0-3724     /opt/homebrew/bin/verible-verilog-syntax
yosys                 ok          0.45+115      /opt/homebrew/bin/yosys
verilator             outdated    5.0.18        /opt/homebrew/bin/verilator  (need ≥ 5.020)
surfer                ok          0.3.0         /opt/homebrew/bin/surfer
sby                   missing     —             —  (optional)
...

Subcommand readiness
----------------------------------------------------------------------
  ok        rb test                 (verible, yosys, verilator, ...)
  outdated  rb test                 (outdated: verilator)
  missing   rb fpv                  (needs: sby)                            (optional feature)
  ...

Hint: `rb tool-check --explain <tool>` for install instructions.
```

The **Tools** section is the per-tool table — name, status (`ok` / `missing` / `outdated`), captured version, resolved path. Python-package detectors show `(python)` in the Path column. A `(need ≥ X)` suffix appears when a tool is present but below `minimum_version`. An `(optional)` suffix appears for tools whose absence does not gate any subcommand.

The **Subcommand readiness** section lists every `rb <subcommand>` whose deps are declared in the manifest. The gloss after each subcommand calls out what is missing or outdated, or lists the participating tools when everything is OK. `(optional feature)` indicates a subcommand whose deps are all optional — `rb wave` is ready even without `gtkwave` installed, for example.

## Subcommand: `--required-for`

```bash
rb tool-check --required-for fpv
```

Narrows the report to just the tools whose `used_by:` includes the named subcommand. Pairs naturally with `--strict` for a "is `rb fpv` ready right now?" CI check:

```bash
rb tool-check --required-for fpv --strict || \
  { echo "rb fpv is not ready — see above"; exit 1; }
```

Exit code semantics under `--required-for` differ slightly from the default — see [Exit codes](#exit-codes) below.

## Subcommand: `--explain`

```bash
rb tool-check --explain surfer
```

Prints the full manifest entry for a single tool — description, used-by subcommands, per-platform install hints, minimum version, and optional notes. Example:

```
surfer — Web-native waveform viewer
  Status:  ok
  Version: 0.3.0
  Path:    /opt/homebrew/bin/surfer
  Used by: rb wave, rb wave-fpv, rb hub
  Install:
    source   https://github.com/rtl-buddy/surfer (branch rtl-buddy)
    build    cd ../surfer && cargo build --release
```

This is also what subcommand wrappers point you at when they refuse to run because a tool is missing — e.g. `rb wave` saying "surfer not found — run `rb tool-check --explain surfer`".

## JSON output

```bash
rb tool-check --format json
```

Emits a structured payload with `tools`, `subcommands`, and a top-level `exit_code` (the same code the process exits with). Schema sketch:

```json
{
  "tools": {
    "verible": { "status": "ok", "version": "v0.0-3724", "path": "/opt/homebrew/bin/...", "optional": false },
    "sby":     { "status": "missing", "version": null, "path": null, "optional": true }
  },
  "subcommands": {
    "fpv":  { "status": "missing", "missing": ["sby"], "outdated": [], "optional_feature": true },
    "test": { "status": "ok", "missing": [], "outdated": [] }
  },
  "exit_code": 1
}
```

JSON output is the wire format for CI agents and IDE integrations — `rb tool-check --format json` is stable enough to script against. Combine with `--required-for` to narrow the result to a single subcommand.

## How the manifest works

The single source of truth lives in `src/rtl_buddy/tool_manifest.py`. Each `ToolSpec` declares:

| Field | Purpose |
|-------|---------|
| `name` | Canonical key used by `--explain`, JSON output, and runtime `require()` |
| `binaries` | Binary names to look for; first one found wins |
| `version_cmd` / `version_regex` | How to probe and parse the installed version |
| `minimum_version` | Lower bound; if violated, status flips to `outdated` |
| `detection` | Ordered detectors (`PathDetector`, `VendorDetector`, `AbsolutePathDetector`, `PythonPackageDetector`, `PythonSiblingDetector`) — first `found=True` wins |
| `install_hint` | Per-platform install instructions for `--explain` |
| `used_by` | Subcommands gated by this tool; drives the readiness section |
| `optional` | If true, missing does not gate subcommand readiness |

The same `ToolSpec` is consulted at runtime when a wrapper invokes `tool_manifest.require("<name>")` — that's how subcommand wrappers produce a uniform "missing tool, see `rb tool-check --explain X`" message instead of an opaque `FileNotFoundError`.

## Reconciliation with `root_config.yaml`

When a project's `root_config.yaml` is discoverable from the current directory, `rb tool-check` reconciles it with the manifest:

- **`cfg-verible`** — the active platform's verible directory is added to verible's detector chain as the *preferred* lookup, with `PATH` retained as fallback.
- **`cfg-surfer`** — the `surfer-default` entry's resolved path is added similarly.
- **`cfg-tools`** — overrides `minimum_version` for any matching tool. Project pins always win over manifest defaults.
- **`cfg-fpv-tools[*].opts.solver-versions`** — pins each FPV solver to an exact version. Runtime semantics is exact-equality (`rb fpv` hard-fails on mismatch); `rb tool-check` surfaces the pin as `minimum_version` so users see a single "outdated" indication for solvers that don't match.

Outside a project (no `root_config.yaml` discoverable), the manifest defaults apply unchanged. The "outside a project" mode is important for first-run setup: `rb tool-check` after `pip install rtl_buddy` is a valid invocation and tells the user what to install before they create a project.

## Version cache

Probed versions are cached to `${XDG_CACHE_HOME:-~/.cache}/rtl_buddy/tool_versions.json` keyed by `(path, mtime)`. The cache makes repeated `rb tool-check` invocations cheap — most tools don't need to be re-probed if their binary hasn't changed. Pass `--no-probe-versions` to skip version probing entirely (faster, but the Version column shows `—` for everything).

## Exit codes

| Exit | Meaning |
|------|---------|
| `0` | All required tools present and up-to-date (optional gaps don't matter) |
| `1` | At least one required tool missing or outdated |
| `2` | `--required-for <sub>` was passed and that subcommand's deps are missing/outdated |

`--strict` is implied for `--required-for` — exit code semantics differ from the default to make the "this one subcommand is broken" case distinguishable from the broader "some tool, somewhere, is missing" case.

## When to use it

- **First-time setup.** Right after `uv add rtl_buddy`, run `rb tool-check` to see which external tools you still need to install for the subcommands you care about.
- **CI gate.** A `rb tool-check --strict` step at the start of a CI job fails fast with an actionable error if the runner image drifted from the expected toolchain.
- **Triaging a "tool not found" error.** When a subcommand wrapper says "X not found — run `rb tool-check --explain X`", that's the canonical entry point.
- **After upgrading a tool.** Re-running `rb tool-check` after `brew upgrade verilator` (etc.) updates the cached version and re-evaluates `minimum_version` checks.

## Out of scope (today)

- **Tool installation.** `rb tool-check` reports state and gives install hints; it does not run installers itself. Treat the install hints as documentation, not as automation.
- **Cross-platform install scripts.** Hints are per-OS (`macos`, `linux`, `source`, `vendor`, `any`); a unified setup script generator is a future possibility but not built today.
- **Custom user manifests.** The manifest is built-in; projects can pin versions and binary paths via `root_config.yaml`, but they cannot add wholly new tools to the manifest. Adding a tool is a code change to `src/rtl_buddy/tool_manifest.py`.
