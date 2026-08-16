---
description: How root_config.yaml configures platform selection, builders, and simulation settings for an RTL project.
---

# Root Config

The `root_config.yaml` file sits at the root of your RTL project and tells `rtl_buddy` how to build and simulate designs on the current platform.

## Location

`rtl_buddy` discovers `root_config.yaml` by walking **up** from the command root (the directory containing the command's primary config — see [Execution Context](execution-context.md)), not from the directory you ran `rb` from. Paths declared inside `root_config.yaml` are resolved relative to the `root_config.yaml` file itself. (Standalone commands that have no primary config — e.g. `rb tool-check` — fall back to walking up from the current directory.)

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
    path: "/opt/homebrew/bin"
    extra_args:
      lint:
        - "--rules=-module-filename"

cfg-rtl-reg:
  reg-cfg-path: "regression.yaml"
```

## Key fields

**`cfg-platforms`**

Maps the current OS (detected via `uname`) to a builder and Verible config. `rtl_buddy` picks the first platform entry whose `unames` list contains the output of `uname`.

A platform entry may also route any *other* tool block by naming one of its entries: `surfer`, `synth-tools`, `pnr-tools`, `power-tools`, `cdc-tools`, `fpv-tools`, `fpga-tools`. Each is optional; a block that is not routed keeps its previous global behaviour. Routing is what lets a path be pinned per platform — a shared Linux tool tree pinned absolutely (`PATH` cannot silently override it, and it survives a `--dispatch slurm` login shell re-prepending site paths) while macOS routes to an entry keeping a bare name off `PATH`:

```yaml
cfg-platforms:
  - os: "linux"
    unames: ["Linux"]
    builder: "verilator-shared"
    verible: "verible-x86_64"
    surfer: "surfer-shared"      # absolute path into the shared tool tree
    fpv-tools: "sby-shared"
  - os: "osx"
    unames: ["Darwin"]
    builder: "verilator"
    verible: "verible-macos"
    surfer: "surfer-brew"        # bare name off PATH
```

Routing supplies the *default* entry for a block; anything that names an entry explicitly — a flow YAML's `tool:`, `--surfer`, `--builder` — still wins.

**Tool paths: `~`, `$VAR`, and candidate lists**

`cfg-rtl-builder[].builder`, `cfg-verible[].path`, `cfg-surfer[].path` and the `tool:` field of every `cfg-*-tools` block are expanded with `expanduser` + `expandvars` before use — the same treatment `cfg-systemc.home` has always had. An unset `${VAR}` makes that value not apply rather than producing a path containing a literal `${...}`.

Each of those fields also accepts a *list* of candidates; the first that expands cleanly and exists wins, and a trailing bare name is the `PATH` fallback:

```yaml
cfg-surfer:
  - name: "surfer-shared"
    path: ["${RB_TOOLS}/bin/surfer", "/opt/rb-tools/current/bin/surfer", "surfer"]
```

That is the full chain a multi-platform project wants — **individual env override → committed canonical path → `PATH`** — expressed in one committed file. The individual half lives in the gitignored [`.rtl-buddy/.env`](#project-local-env-defaults-rtl-buddyenv) (`RB_TOOLS=/Users/me/tools/rb`), so a developer relocating their copy never dirties a tracked file, and everyone else gets the canonical path. CI wants the pin hard enough that a stray `PATH` cannot change it and laptops want it soft enough to relocate; expansion with a committed fallback satisfies both, because the value is still decided by a committed file and the only thing an individual supplies is *where their copy lives*.

**`cfg-rtl-builder`**

Defines simulation tool configurations. Each entry has:

- `builder`: simulator executable name (`verilator`, `vcs`, etc.), a path to it, or a candidate list
- `builder-simv`: path to the compiled simulation binary
- `sim-rand-seed` / `sim-rand-seed-prefix`: default seed value and the plusarg prefix used to pass it
- `builder-opts`: named compile-time and run-time option sets, selected by builder mode

**`cfg-verible`**

Defines Verible tool configurations for lint and syntax checks. `path` is the directory containing Verible executables — absolute or relative to `root_config.yaml`. If that directory is missing but `verible-verilog-syntax` is on `PATH`, Verible stays enabled and rtl_buddy warns, naming both the configured directory and what `PATH` resolved: a deliberately pinned path silently resolving elsewhere is exactly what pinning is meant to rule out.

**`cfg-surfer`** *(optional)*

Configures the Surfer waveform viewer for `rb wave`. Fields:

- `path`: bare executable name (resolved via PATH, e.g. `"surfer"`) or a relative/absolute path to the binary
- `wcp-port`: TCP port rtl-buddy listens on; Surfer connects with `--wcp-initiate` (default: `0` — OS auto-assigns a free port)
- `editor-cmd`: command template with `%f` (file path) and `%l` (line number) placeholders — e.g. `"vim +%l %f"`, `"code --goto %f:%l"`
- `editor-terminal`: how to open terminal editors — `tmux` (new tmux window), `iterm2`, `terminal` (macOS Terminal.app), or `""` to run the command directly (for GUI editors)
- `editor-sock`: path to a Unix socket for nvim remote reuse (e.g. `"/tmp/nvim-rb.sock"`). When set, rtl-buddy launches nvim with `--listen <sock>` on first use and reuses the already-running instance for subsequent "Go to declaration" and cursor-moved events. Omit this field if you do not use nvim or do not want remote reuse.

`rb wave <test>` looks for a signal layout file at `<test>.surfer` in the same directory as `tests.yaml` (e.g. `verif/sandbox/basic.surfer`). If found it is passed to Surfer via `-c`; if not, Surfer opens with no pre-loaded signals. If no FST exists for the test, `rb wave` runs a debug sim automatically before launching Surfer.

### Signal value annotation with nvim

When `editor-sock` is set and the nvim plugin is installed, `rb wave` annotates signal values as end-of-line virtual text in nvim:

- Right-click a signal in Surfer and choose "Go to declaration": nvim opens at the signal's declaration and all signals in the same module scope are annotated with their waveform values (`▶ value [instance]` style, black text on a lemon-chiffon background using the `WaveValue` highlight group).
- Moving the Surfer time cursor updates all annotations in real time.
- Two signals that share a source line are combined into a single annotation: `▶ a=val  b=val [inst]`.
- Pass `--focused-signal` to `rb wave` to annotate only the signal explicitly selected via "Go to declaration" instead of the full module scope.

**Installing the nvim plugin:**

```bash
rb nvim-install          # install the unified rtl-buddy-nvim plugin (hub + annotation)
rb nvim-install --update # sync an existing install to the pinned revision
rb nvim-install --force  # remove and re-install
```

This installs the [`rtl-buddy-nvim`](https://github.com/rtl-buddy/rtl-buddy-nvim) plugin — the same plugin that provides the [hub](hub.md) connection — which supplies the `WaveValue` highlight group and the annotation hook. One command wires both the annotation and the hub auto-connect; see [the wave doc](wave.md#nvim-setup) for details.

**`cfg-rtl-reg`**

Sets the default path to `regression.yaml` used by `rtl-buddy regression` when `--reg-config` is not specified.

## Builder and mode overrides

Use command-line flags to override the platform defaults for a run:

- `--builder b`: use a different builder (e.g. `--builder vcs`)
- `--builder-mode m`: use a different named option set (e.g. `--builder-mode reg`)

See the [CLI reference](../reference/cli.md) for the full option list.

## Project-local env defaults: `.rtl-buddy/.env`

Some values are project-scoped but machine-local — they belong with the project, yet committing them would break every other checkout (an absolute `RTL_BUDDY_SLANG_PLUGIN`, a `SYSTEMC_HOME`). Put them in `.rtl-buddy/.env` next to `root_config.yaml`:

```sh
# .rtl-buddy/.env — KEY=VALUE per line; # comments; `export ` prefix tolerated
RTL_BUDDY_SLANG_PLUGIN=/opt/rtl-buddy-tools/yosys-slang/build/slang.so
SYSTEMC_HOME=/opt/homebrew/opt/systemc
```

Every `rb` command loads the file as soon as the project root is discovered and injects the variables into its environment, so both rtl_buddy itself and every tool subprocess (yosys, sby, verilator, the compiled simv) see them.

Semantics:

- **Fallback only.** A variable already present in the process environment is never overridden — the shell, CI, and sourced toolchain env scripts always win, and explicit YAML config (e.g. `plugin-path`) beats any environment source.
- **Literal values.** No `$VAR` interpolation, no escapes; surrounding matching quotes are stripped. A line that is not `KEY=VALUE` fails loud with the file and line number.
- **Untracked by design.** Add `.rtl-buddy/.env` to `.gitignore` (`rtl-buddy skill print-gitignore` includes it). A committed env file would inject machine paths — or worse, loader variables — into every clone's runs.

This completes the lookup chain that [`plugin-path`](../reference/yaml.md#root_configyaml) uses: per-project YAML config, then the process environment (set machine-wide by a toolchain env script), then `.rtl-buddy/.env` for project-scoped machine-local values.

## Full schema

See [YAML Formats: root_config.yaml](../reference/yaml.md#root_configyaml) for the complete field reference.
