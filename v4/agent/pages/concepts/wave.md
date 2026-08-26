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
    editor-cmd: "nvim +%l %f"               # %f = file, %l = line
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

The annotation feature requires a small nvim plugin. Install it once:

```bash
rb wave-install-nvim
```

This copies `rtl_buddy_wave.lua` to `~/.local/share/nvim/site/plugin/`, which nvim auto-sources at startup. No `init.lua` changes are needed. Reinstall after rtl-buddy upgrades with `--force`:

```bash
rb wave-install-nvim --force
```

If the plugin is missing when `rb wave` starts with `editor-sock` configured, a warning is shown:

```
WARNING  nvim plugin not installed — run "rb wave-install-nvim" to enable wave annotations
```

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

## Surfer build

The annotation features require Surfer built from the `rtl-buddy` branch:

```bash
git clone https://github.com/rtl-buddy/surfer.git ../surfer
cd ../surfer && git checkout rtl-buddy
cargo build --release
```

Point `cfg-surfer.path` at `../surfer/target/release/surfer` (relative to `root_config.yaml`) or install the binary on `PATH`.
