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
