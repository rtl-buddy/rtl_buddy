## Understand the manifest

`src/rtl_buddy/tool_manifest.py` is the source of truth for both reports and runtime dependency errors. Each tool declares its canonical name and aliases, ordered detection methods, version probe and minimum, install hints, dependent subcommands, and whether it is optional.

The first successful detector wins. Detectors cover `PATH`, configured absolute or vendor paths, Python packages, and sibling Python distributions. Manifest construction rejects name or alias collisions.

Runtime wrappers call the same manifest and produce a consistent recovery hint:

```text
<tool> not found — run `rb tool-check --explain <tool>` for install instructions
```

`rb tool-check` diagnoses and explains dependencies; it does not install tools or accept project-defined tool specifications. Projects may override known tool paths and versions through `root_config.yaml`. See [YAML formats](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#root_configyaml) and the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/cli/).
