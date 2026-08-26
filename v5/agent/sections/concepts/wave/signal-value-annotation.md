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
