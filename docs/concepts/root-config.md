---
description: Configure project-wide platforms, simulation builders, tool paths, regression defaults, and machine-local environment values in root_config.yaml.
---

# Root Config

`root_config.yaml` holds the project-wide platform and tool configuration. Use it to map hosts to simulator builders, locate tools, and set regression defaults.

## Place and discover the config

Keep `root_config.yaml` at the project root. RTL Buddy walks upward from the command root, the directory containing the primary command config, and uses the first root config it finds. A command with no primary config walks upward from the shell's current directory.

Paths inside `root_config.yaml` resolve from its directory. See [Execution Context](execution-context.md) for command-root behavior.

## Configure a simulation platform

A minimal configuration maps the host `uname` to a builder:

```yaml
rtl-buddy-filetype: project_root_config

cfg-platforms:
  - os: osx
    unames: [Darwin]
    builder: verilator
    verible: verible-macos
    surfer: surfer-default

cfg-rtl-builder:
  - name: verilator
    builder: verilator
    builder-simv: obj_dir/simv
    sim-rand-seed: 31310
    sim-rand-seed-prefix: +verilator+seed+
    builder-opts:
      debug:
        compile-time: --binary -sv -o simv
        run-time: +verilator+rand+reset+2
      reg:
        compile-time: --binary -sv -o simv
        run-time: +verilator+rand+reset+2

cfg-verible:
  - name: verible-macos
    path: /opt/homebrew/bin

cfg-surfer:
  - name: surfer-default
    path: surfer

cfg-rtl-reg:
  reg-cfg-path: regression.yaml
```

- If several platform entries match `uname`, the last match wins. Routing names are validated on every platform entry at load time, including entries for other hosts.
- A platform routes simulation builders, Verible, and Surfer. It cannot route `cfg-synth-tools`, `cfg-pnr-tools`, `cfg-power-tools`, `cfg-cdc-tools`, `cfg-fpv-tools`, or `cfg-fpga-tools`; each flow's `tool:` field selects those entries directly.
- `--builder`, `--builder-mode`, or a flow-specific option overrides the platform default for one command. See the [CLI reference](../reference/cli.md).

## Configure portable tool paths

Executable fields accept a bare name, a relative or absolute path, or an ordered list of candidates:

```yaml
cfg-surfer:
  - name: surfer-default
    path:
      - ${RB_TOOLS}/bin/surfer
      - /opt/rb-tools/current/bin/surfer
      - surfer
```

RTL Buddy expands `~` and environment variables and picks the first candidate that exists and is executable. Relative paths resolve from `root_config.yaml`, and a bare name falls back to `PATH`. A candidate containing an unset variable is skipped. A candidate list lets you combine a machine override, a committed shared-tool path, and a `PATH` fallback without editing tracked YAML.

This applies to `cfg-rtl-builder[].builder`, `cfg-surfer[].path`, the tool fields in `cfg-*-tools`, and `cfg-verible[].path`.

`cfg-verible[].path` names a directory, not a binary. A bare value is a directory relative to the root config, not a `PATH` lookup. If that directory lacks a requested Verible executable, RTL Buddy warns and may use the one on `PATH`.

## Configure the builder

Each `cfg-rtl-builder` entry sets:

- the simulator executable and the compiled `simv` path;
- the simulator family and seed syntax;
- named compile-time and run-time option sets;
- optionally, timeout allowances and the waveform format.

A test's builder comes from the CLI, then the test or suite config, then the platform default. See [Simulation Backends](simulators.md#select-a-builder) and the [root config schema](../reference/yaml.md#root_configyaml). Surfer editor and socket settings live under `cfg-surfer`; see [Waveform Viewer](wave.md#configure-surfer-and-the-editor).

## Set regression defaults

`cfg-rtl-reg` gives fallback paths for simulation and flow regression manifests. An explicit `-c` option, then a matching manifest in the invocation directory, takes precedence. See [Regressions](regressions.md#resolve-the-manifest).

## Project-local env defaults: `.rtl-buddy/.env`

Put untracked, machine-specific values in `.rtl-buddy/.env` beside `root_config.yaml`:

```sh
RTL_BUDDY_SLANG_PLUGIN=/opt/rtl-buddy-tools/yosys-slang/build/slang.so
SYSTEMC_HOME=/opt/homebrew/opt/systemc
RB_TOOLS=/Users/me/tools/rtl-buddy
```

Every command loads this file after finding the project root and passes the values to tool subprocesses.

- Variables already in the process environment win; the file only supplies fallbacks.
- Explicit YAML configuration wins over the environment fallback where a field supports both.
- Lines are `KEY=VALUE`, with `#` comments and an optional `export ` prefix. Values are literal: no interpolation or escapes, and matching surrounding quotes are removed.
- A malformed line fails with its file and line number.
- Add `.rtl-buddy/.env` to `.gitignore`. `rb skill print-gitignore` prints the recommended entry.

## Use the schema reference

[YAML Formats: root_config.yaml](../reference/yaml.md#root_configyaml) lists every supported block and field.
