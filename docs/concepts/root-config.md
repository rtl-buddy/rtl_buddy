---
description: Configure project-wide platforms, simulation builders, tool paths, regression defaults, and machine-local environment values in root_config.yaml.
---

# Root Config

`root_config.yaml` defines project-wide platform and tool configuration.

## Place and discover the config

Keep `root_config.yaml` at the project root. RTL Buddy walks upward from the command root—the directory containing the primary command config—and uses the first root config it finds. Commands without a primary config walk upward from the shell's current directory.

Paths inside `root_config.yaml` resolve from its directory. See [Execution Context](execution-context.md) for command-root behavior.

## Configure a simulation platform

A minimal simulation configuration maps the host `uname` to a builder:

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

If multiple platform entries match `uname`, the last match wins. RTL Buddy validates routing names on every platform entry at load time, including entries for other hosts.

A platform may route simulation builders, Verible, and Surfer. It cannot route `cfg-synth-tools`, `cfg-pnr-tools`, `cfg-power-tools`, `cfg-cdc-tools`, `cfg-fpv-tools`, or `cfg-fpga-tools`; each flow's `tool:` field selects those entries directly.

Override the platform defaults for one command with `--builder`, `--builder-mode`, or the flow-specific CLI option. See the [CLI reference](../reference/cli.md).

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

RTL Buddy expands `~` and environment variables, then chooses the first executable candidate that exists. Relative paths resolve from `root_config.yaml`; a bare name falls back to `PATH`. A candidate containing an unset variable is skipped.

This applies to `cfg-rtl-builder[].builder`, `cfg-surfer[].path`, tool fields in `cfg-*-tools`, and `cfg-verible[].path`. The Verible field names a directory rather than a binary, so a bare value is a root-config-relative directory, not a `PATH` lookup. If the configured directory cannot supply a requested Verible executable, RTL Buddy warns and may use the executable found on `PATH`.

Use candidate lists to combine a machine override, a committed shared-tool path, and a `PATH` fallback without editing tracked YAML.

## Configure the builder

Each `cfg-rtl-builder` entry owns:

- the simulator executable and compiled `simv` path;
- simulator family and seed syntax;
- named compile-time and run-time option sets;
- optional builder-specific timeout allowances and waveform format.

Tests select a builder through the CLI, test or suite config, then platform default. See [Simulation Backends](simulators.md#select-a-builder) and the [root config schema](../reference/yaml.md#root_configyaml).

Keep Surfer editor and socket settings under `cfg-surfer`; [Waveform Viewer](wave.md#configure-surfer-and-the-editor) owns that workflow.

## Set regression defaults

`cfg-rtl-reg` supplies fallback paths for simulation and flow regression manifests. An explicit `-c` option and a matching manifest in the invocation directory take precedence. See [Regressions](regressions.md#resolve-the-manifest).

## Project-local env defaults: `.rtl-buddy/.env`

Store project-specific, untracked machine values in `.rtl-buddy/.env` beside `root_config.yaml`:

```sh
RTL_BUDDY_SLANG_PLUGIN=/opt/rtl-buddy-tools/yosys-slang/build/slang.so
SYSTEMC_HOME=/opt/homebrew/opt/systemc
RB_TOOLS=/Users/me/tools/rtl-buddy
```

Every command loads this file after discovering the project root and passes the values to tool subprocesses.

- Existing process environment variables win; the file provides fallback values only.
- Values are literal. There is no variable interpolation or escape processing; matching surrounding quotes are removed.
- Lines must use `KEY=VALUE`; comments and an optional `export ` prefix are accepted.
- Add `.rtl-buddy/.env` to `.gitignore`. `rb skill print-gitignore` prints the recommended entry.

Explicit YAML configuration still wins over environment fallback where a field supports both. A malformed env line fails with its file and line number.

## Use the schema reference

See [YAML Formats: root_config.yaml](../reference/yaml.md#root_configyaml) for all supported blocks and fields.
