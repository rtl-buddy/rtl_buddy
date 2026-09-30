## Apply project configuration

Inside a project, tool-check merges the built-in tool list with `root_config.yaml`:

- `cfg-verible` and the active `cfg-surfer` entry add preferred locations; `PATH` remains a fallback. Absolute paths work.
- `cfg-tools` overrides minimum versions. An entry qualified by platform applies only to that OS and beats an unqualified one.
- `cfg-fpv-tools[*].opts.solver-versions` sets solver versions. FPV runs require exact equality; tool-check shows a mismatch as `outdated`.
- Other `cfg-*-tools` blocks are not consulted, because each flow picks its entry at run time.

Without `root_config.yaml`, built-in locations and version floors apply. See [YAML formats](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#root_configyaml) and the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/cli/).
