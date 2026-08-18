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

Maps the current OS (detected via `uname`) to a builder and Verible config. `rtl_buddy` picks the **last** platform entry whose `unames` list contains the output of `uname` — with the usual one-entry-per-uname layout there is only one match, but overlapping `unames` lists are resolved last-wins.

A platform entry may also route `cfg-surfer` by naming one of its entries. It is optional; unrouted, `cfg-surfer` keeps its previous global behaviour (the `surfer-default` entry). Routing lets the viewer be pinned per platform — a shared Linux tool tree pinned absolutely (`PATH` cannot silently override it, and it survives a `--dispatch slurm` login shell re-prepending site paths) while macOS routes to an entry keeping a bare name off `PATH`:

```yaml
cfg-platforms:
  - os: "linux"
    unames: ["Linux"]
    builder: "verilator-shared"
    verible: "verible-x86_64"
    surfer: "surfer-shared"      # absolute path into the shared tool tree
  - os: "osx"
    unames: ["Darwin"]
    builder: "verilator"
    verible: "verible-macos"
    surfer: "surfer-brew"        # bare name off PATH
```

Routing supplies the *default* entry for a block; a CLI flag that names one explicitly — `--surfer`, `--builder` — still wins. Every platform entry's routing is validated at load, not only the one matching this host, so a typo in the Linux entry fails on a developer's laptop rather than waiting for CI.

The `cfg-*-tools` blocks (`cfg-synth-tools`, `cfg-pnr-tools`, `cfg-power-tools`, `cfg-cdc-tools`, `cfg-fpv-tools`, `cfg-fpga-tools`) are **not** routable, and naming one on a platform entry is a fatal config error rather than a silent no-op. Their entry is chosen per run by the flow YAML's `tool:`, and that name simultaneously selects the *backend* — `openroad` picks the OpenROAD P&R backend, `yosys` the Yosys synthesis backend, `rb power` looks the name up in a backend registry — so a platform-level redirect could only be ignored (the flow already named an entry) or break dispatch (the routed name is not a backend). To pin one of those binaries per platform, pin it in the entry itself with the candidate list `tool:` accepts, described next: the first candidate that exists wins, so a Linux tool-tree path and a Homebrew path can share one committed entry and each host takes the one it has.

**Tool paths: `~`, `$VAR`, and candidate lists**

`cfg-rtl-builder[].builder`, `cfg-verible[].path`, `cfg-surfer[].path` and the `tool:` field of every `cfg-*-tools` block are expanded with `expanduser` + `expandvars` before use — the same treatment `cfg-systemc.home` has always had. An unset `${VAR}` makes that value not apply rather than producing a path containing a literal `${...}`.

Each of those fields also accepts a *list* of candidates; the first that expands cleanly and exists wins, and a trailing bare name is the `PATH` fallback. "Exists" means an *executable file* for the binary-valued fields, so a candidate that is present but not runnable falls through to the next one instead of winning and then being reported unavailable; relative candidates are tested against the directory holding `root_config.yaml`, never the process cwd:

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

Defines Verible tool configurations for lint and syntax checks. `path` is the directory containing Verible executables — absolute or relative to `root_config.yaml`. If that directory is missing — or is present but does not contain the binary — Verible stays enabled off `PATH` and rtl_buddy warns, naming both the configured location and what `PATH` resolved: a deliberately pinned path silently resolving elsewhere is exactly what pinning is meant to rule out. Because `path` names a *directory*, a candidate without a path separator is a relative directory next to `root_config.yaml`, never a `PATH` lookup.

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
