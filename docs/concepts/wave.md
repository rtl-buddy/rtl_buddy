---
description: How rb wave opens Surfer with live signal value annotation in your editor via the WCP protocol.
---

# Waveform Viewer (`rb wave`)

> **Integration type:** Integrated tool. `rb wave` is built around Surfer today; Vaporview / VS Code support is on the roadmap — tracked in [issue #84](https://github.com/rtl-buddy/rtl_buddy/issues/84).
>
> **External binary required:** Surfer, built from the [`rtl-buddy/surfer`](https://github.com/rtl-buddy/surfer) fork on the `rtl-buddy` branch. Mainline Surfer works for basic FST viewing but not for WCP signal-value annotation. See [Surfer build](#surfer-build).
>
> **Editor integration:** nvim for the full annotation round-trip (declaration jump + live signal values + add-from-editor). Any editor can be configured via `editor-cmd` for one-way "open at line".
>
> See also: [Installation — External tools by feature](../install.md#external-tools-by-feature).

`rb wave` opens the [Surfer](https://surfer-project.org/) waveform viewer for a test and connects it to your editor via the **WCP** (Waveform Client Protocol). When you right-click a signal in Surfer and choose **Go to declaration**, rtl-buddy:

1. Resolves the signal to its source file and line number
2. Opens (or reuses) your editor at that location
3. Annotates the signal's waveform value at the current cursor position as inline virtual text

## Basic usage

```bash
cd verif/sandbox
uv run rb wave basic          # runs debug sim if no FST exists, then opens Surfer
uv run rb wave basic --resim  # force re-run of debug sim
```

Signal layout files are loaded automatically: if `basic.surfer` exists next to `tests.yaml`, Surfer opens with those signals pre-loaded.

## Configuration

Add a `cfg-surfer` section to `root_config.yaml`:

```yaml
cfg-surfer:
  - name: "surfer-default"
    path: "../surfer/target/release/surfer"  # or bare name on PATH
    wcp-port: 0                              # 0 = OS assigns a free port
    editor-cmd: "nvim +%l %f"               # %f = file, %l = line (schema default is "vim +%l %f")
    editor-terminal: "tmux"                  # tmux | iterm2 | terminal | ""
    editor-sock: "~/.local/share/rtl-buddy/wave-nvim.sock"  # enables nvim reuse
    ctrl-sock: "~/.local/share/rtl-buddy/wave-ctrl.sock"    # enables nvim → Surfer
```

See [YAML Formats](../reference/yaml.md) for all fields.

## Signal value annotation

### How it works

When a `goto_declaration` event arrives from Surfer, rtl-buddy:

1. Reads the signal value at the cursor timestamp from the FST via **pywellen**
2. Enumerates all other signals in the same module scope using the FST hierarchy
3. Runs a **single bulk grep** across the SV source files to map every signal to its declaration line (result cached for the session)
4. Pushes all values to the editor as EOL virtual text

Moving the Surfer time cursor fires a `cursor_moved` WCP event, which re-reads all scope signal values and updates the annotations live — no interaction required.

Two signals declared on the same source line are combined into one annotation:

```systemverilog
logic a, b;    ▶ a=8'h0a  b=8'h05 [i_dut]
logic clk;     ▶ 1'b0 [i_dut]
```

### Active scope

Clicking any signal in Surfer's signal list sets the **active scope** — the module instance used to resolve signal names. rtl-buddy updates the scope cache automatically via the `scope_changed` WCP event, so annotation context is always current without requiring a "Go to declaration".

### nvim setup

The annotation feature lives in the [`rtl-buddy-nvim`](https://github.com/rtl-buddy/rtl-buddy-nvim) plugin — the same plugin that connects your editor to the [hub](hub.md). Install it once with the unified command:

```bash
rb nvim-install
```

This clones the plugin (pinned to a revision compatible with this rtl-buddy's hub protocol) into `~/.local/share/nvim/site/pack/rtlbuddy/start/rtl-buddy-nvim` and writes a managed `~/.local/share/nvim/site/plugin/rtl_buddy_setup.lua` that nvim auto-sources at startup — no `init.lua` changes are needed. The managed setup also auto-connects to the hub and, when `verible-verilog-ls` is on `PATH`, starts it for symbol resolution.

Keep it current after an rtl-buddy upgrade:

```bash
rb nvim-install --update   # sync to the revision pinned by your rtl-buddy
rb nvim-install --force    # remove and re-install
```

`git` is required (the plugin is fetched via `git clone`). For an offline or sibling-checkout install, point at a local path: `rb nvim-install --source /path/to/rtl-buddy-nvim --ref <branch>`.

If the plugin is missing when `rb wave` starts with `editor-sock` configured, a warning is shown:

```
WARNING  nvim plugin not installed — run "rb nvim-install" to enable the hub connection and wave annotations
```

Verify the install with `:checkhealth rtlbuddy` in nvim — it reports hub state, LSP attach, and whether wave annotation is enabled.

### Adding signals to Surfer from nvim

With `ctrl-sock` configured, place the cursor on any signal name in nvim and press **`<Space>wa`** (`<leader>wa`) to add it to Surfer's waveform view.

The signal is resolved using the active scope — click a signal in Surfer first to establish the instance context (e.g. clicking `tb_top.i_dut.clk` sets scope `tb_top.i_dut`), then add signals freely from nvim.

```
nvim: cursor on "rst"  →  <Space>wa  →  Surfer adds tb_top.i_dut.rst to waveform
```

The keymap requires `ctrl-sock` to be set in `cfg-surfer` and `rb wave` to be running. A warning is shown if the socket is unreachable.

### Single-signal mode

To annotate only the signal you right-clicked (not the whole scope):

```bash
uv run rb wave basic --focused-signal
```

### Editor socket reuse

When `editor-sock` is set, rtl-buddy launches nvim with `--listen <sock>` on first use. Subsequent `goto_declaration` and `cursor_moved` events reuse the running instance via `--remote-expr nvim_exec2(...)` — no new windows, no command-line flicker.

The socket is probed with a 300 ms timeout. If the socket is stale (nvim has been closed), the next `goto_declaration` opens a fresh nvim window.

## Opening FPV counterexamples (`rb wave-fpv`)

`rb wave-fpv <verif_name>` opens the SymbiYosys counterexample VCD for a failed [formal verification](fpv.md) in Surfer:

```bash
uv run rb wave-fpv demo_fpv_counter_safety
```

It reads the same `fpv.yaml` (`-c`/`--fpv-config`, default `fpv.yaml`) to resolve the verification name, then opens the trace at `<dir of fpv.yaml>/artefacts/<verif>/sby_workdir/engine_<N>/trace.vcd` (first engine in sorted order). It opens the VCD in the `cfg-surfer` entry named `surfer-default` unless you pass `--surfer <name>`. Unlike `rb wave`, it just opens the VCD — there is no WCP annotation round-trip — so mainline Surfer suffices. It raises a clear error if the verification has not been run, the proof passed (no counterexample), or no engine produced a trace.

## Hub integration

When a project [coordination hub](hub.md) is running, `rb wave` opportunistically connects to it as the `wave`-origin peer (the bridge in `tools/wave_hub_bridge.py`; the hub is discovered via `$RTL_BUDDY_HUB` or by walking up to `.rtl-buddy/hub.json`). The bridge forwards Surfer events to the hub (cursor moves → `cursor_time_changed`, plus `scope_changed`, `signal_selected`, and a `wave_values_changed` snapshot on cursor move) and serves hub requests back to Surfer. Navigation requests are `wave_set_cursor`, `wave_set_scope`, `wave_set_viewport`, `wave_zoom_to_range`, `wave_zoom_to_fit`; item-management requests are `wave_add_variables`, `wave_get_items`, `wave_remove_items`, `wave_move_items`, and `wave_add_comments`. The item-management requests let an agent (over `rb hub send`) inspect and curate the displayed signal/comment list, with genuine success/error reporting — the bridge waits for Surfer's WCP `ack`/response and translates a Surfer `error` frame into a hub `error` instead of replying optimistically. (`wave_move_items` and `wave_add_comments` require the surfer fork's `move_items` / `add_dividers` WCP commands.) Time is exchanged in femtoseconds (`time_unit=fs`). If no hub is reachable the bridge stays silent and `rb wave` runs fully standalone — the hub is never required.

## Time units

The wave stack uses **two different time units** — mixing them silently mis-places the cursor by orders of magnitude:

- **pywellen / FST analysis — timescale *ticks*.** Signal-value reads go through pywellen, whose `Signal.all_changes()` and `Waveform.time_table` return integer **timesteps in the FST's timescale resolution**, not picoseconds. With Verilator's default `1ns/10ps` timescale one tick = 10 ps, so a 10 ns clock period is 1000 ticks and a cursor at 95 ns is tick 9500. Convert ticks to real time with the timescale object from `Waveform.hierarchy.timescale()` (a method — it returns `.factor` / `.unit`, e.g. `10` / `ps`), not a bare multiplier.
- **Hub / WCP — femtoseconds.** The bridge and `rb hub send wave-cursor <T_FS>` / `wave-zoom <START_FS> <END_FS>` exchange time in **femtoseconds** (`time_unit=fs`); the bridge forwards the fs value to Surfer verbatim with `time_unit=fs` and Surfer converts to its own timestep. At a 10 ps timescale 1 tick = 10 000 fs, so 95 ns = 95 000 000 fs = tick 9500. Pass fs to the `wave-*` verbs — a ps or tick value lands 10³–10⁶× off.
- **Surfer command files (the [`rtl-buddy/surfer`](https://github.com/rtl-buddy/surfer) fork) — timescale ticks.** `cursor_set` / `zoom_range` in a `-c` command file use the same tick unit as pywellen, *not* fs. (This is surfer-side behavior, documented here for the round-trip.)

## Surfer build

The annotation features require Surfer built from the `rtl-buddy` branch:

```bash
git clone https://github.com/rtl-buddy/surfer.git ../surfer
cd ../surfer && git checkout rtl-buddy
cargo build --release
```

Point `cfg-surfer.path` at `../surfer/target/release/surfer` (relative to `root_config.yaml`) or install the binary on `PATH`.
