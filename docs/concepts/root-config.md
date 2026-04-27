---
description: How root_config.yaml configures platform selection, builders, and simulation settings for an RTL project.
---

# Root Config

The `root_config.yaml` file sits at the root of your RTL project and tells `rtl_buddy` how to build and simulate designs on the current platform.

## Location

`rtl_buddy` looks for `root_config.yaml` in the current working directory. All paths in the config are resolved relative to where `rtl_buddy` is invoked.

## Structure

```yaml
rtl-buddy-filetype: project_root_config

cfg-platforms:
  - os: "osx"
    unames: ["Darwin"]
    builder: "verilator"
    verible: "verible-macos"

cfg-rtl-builder:
  - name: "verilator"
    builder: "verilator"
    builder-simv: "obj_dir/simv"
    sim-rand-seed: 31310
    sim-rand-seed-prefix: "+verilator+seed+"
    builder-opts:
      debug:
        compile-time: "--binary -sv -o simv"
        run-time: "+verilator+rand+reset+2"
      reg:
        compile-time: "--binary -sv -o simv"
        run-time: "+verilator+rand+reset+2"

cfg-verible:
  - name: "verible-macos"
    path: "tools/verible/macos/active/bin"
    extra_args:
      lint:
        - "--rules=-module-filename"

cfg-rtl-reg:
  reg-cfg-path: "design/regression.yaml"
```

## Key fields

**`cfg-platforms`**

Maps the current OS (detected via `uname`) to a builder and Verible config. `rtl_buddy` picks the first platform entry whose `unames` list contains the output of `uname`.

**`cfg-rtl-builder`**

Defines simulation tool configurations. Each entry has:

- `builder`: simulator executable name (`verilator`, `vcs`, etc.)
- `builder-simv`: path to the compiled simulation binary
- `sim-rand-seed` / `sim-rand-seed-prefix`: default seed value and the plusarg prefix used to pass it
- `builder-opts`: named compile-time and run-time option sets, selected by builder mode

**`cfg-verible`**

Defines Verible tool configurations for lint and syntax checks.

**`cfg-rtl-reg`**

Sets the default path to `regressions.yaml` used by `rtl-buddy regression` when `--reg-config` is not specified.

## Builder and mode overrides

Use command-line flags to override the platform defaults for a run:

- `--builder b`: use a different builder (e.g. `--builder vcs`)
- `--builder-mode m`: use a different named option set (e.g. `--builder-mode reg`)

See the [CLI reference](../reference/cli.md) for the full option list.

## Full schema

See [YAML Formats: root_config.yaml](../reference/yaml.md#root_configyaml) for the complete field reference.
