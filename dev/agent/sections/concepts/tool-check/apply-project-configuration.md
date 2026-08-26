## Apply project configuration

When a project is discoverable, tool-check reconciles the built-in manifest with `root_config.yaml`:

- `cfg-verible` and the active `cfg-surfer` entry add preferred detectors while retaining `PATH` fallback. Absolute paths are supported.
- `cfg-tools` overrides minimum versions. Platform-qualified entries apply only to the matching configured OS and take precedence over unqualified entries.
- `cfg-fpv-tools[*].opts.solver-versions` supplies solver version expectations. Runtime FPV checks exact equality; tool-check presents a mismatch as outdated.
- Other `cfg-*-tools` blocks do not select a detector because each flow chooses its entry at run time. A flow's pinned `tool:` path is honored when that flow runs.

Without `root_config.yaml`, built-in detectors and version floors apply.

Detected versions are cached at `${XDG_CACHE_HOME:-~/.cache}/rtl_buddy/tool_versions.json`, keyed by binary path and modification time. Use `--no-probe-versions` for a faster presence-only check; versions then display as unknown.
