## Understand the manifest

`src/rtl_buddy/tool_manifest.py` is the source of truth for both reports and runtime dependency errors. Each tool declares its canonical name and aliases, its required binaries, ordered detection methods, version probe and minimum, install hints, dependent subcommands, whether it is optional, and any optional binaries.

`binaries` is the tool's required core, and it is an any-of list: the first name found on `PATH` (or in a configured vendor directory) makes the tool detected, and that resolved path is substituted into the version probe. A binary that does not by itself make the tool usable therefore does not belong there — listing one would let a host missing every real command report `ok`, version-probed through the wrong executable. Such helpers go in `optional_binaries`, a mapping of binary name to what it buys, which only `--explain` reads.

The first successful detector wins. Detectors cover `PATH`, configured absolute or vendor paths, Python packages, and sibling Python distributions. Manifest construction rejects name or alias collisions.

Runtime wrappers call the same manifest and produce a consistent recovery hint:

```text
<tool> not found — run `rb tool-check --explain <tool>` for install instructions
```

`rb tool-check` diagnoses and explains dependencies; it does not install tools or accept project-defined tool specifications. Projects may override known tool paths and versions through `root_config.yaml`. See [YAML formats](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#root_configyaml) and the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/v6/reference/cli/).
