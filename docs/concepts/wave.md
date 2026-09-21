---
description: Open simulation and formal waveforms in Surfer, configure editor navigation, and use live signal annotation safely.
---

# Waveform Viewer

`rb wave` opens a test waveform in Surfer. With the RTL Buddy Surfer fork and nvim plugin, it also supports source navigation, live value annotations, and adding signals from the editor.

## Install Surfer

Basic FST and VCD viewing works with mainline Surfer. Live editor annotation requires the `rtl-buddy` branch of the [RTL Buddy Surfer fork](https://github.com/rtl-buddy/surfer/tree/rtl-buddy):

```bash
git clone https://github.com/rtl-buddy/surfer.git ../surfer
cd ../surfer
git checkout rtl-buddy
cargo build --release
```

Put the binary on `PATH` or configure its path in `cfg-surfer`.

## Open a test waveform

From a suite containing `tests.yaml`:

```bash
uv run rb wave basic
uv run rb wave basic --resim
```

If no waveform exists, the first command runs the test in debug mode. `--resim` always reruns it. RTL Buddy opens the newest supported FST or VCD under the test artefacts.

If `basic.surfer` exists beside `tests.yaml`, RTL Buddy passes it to Surfer as the initial signal layout.

## Configure Surfer and the editor

Add a named entry to `root_config.yaml` and route it from the active platform:

```yaml
cfg-platforms:
  - os: osx
    unames: [Darwin]
    builder: verilator
    surfer: surfer-default

cfg-surfer:
  - name: surfer-default
    path: ../surfer/target/release/surfer
    wcp-port: 0
    editor-cmd: nvim +%l %f
    editor-terminal: tmux
    editor-sock: ~/.local/share/rtl-buddy/wave-nvim.sock
    ctrl-sock: ~/.local/share/rtl-buddy/wave-ctrl.sock
```

`%f` and `%l` expand to the source file and line. `wcp-port: 0` lets the OS select a free port. `editor-sock` enables nvim reuse and annotations; `ctrl-sock` enables editor-to-Surfer actions. Omit the sockets when using another editor for one-way source navigation.

See [YAML Formats](../reference/yaml.md#root_configyaml) for all fields.

## Install the nvim integration

```bash
rb nvim-install
rb nvim-install --update
```

The command installs a compatible revision of `rtl-buddy-nvim` and writes an auto-loaded setup file; no `init.lua` change is required. It needs Git and network access. For an offline checkout:

```bash
rb nvim-install --source /path/to/rtl-buddy-nvim --ref <branch>
```

Use `--force` to replace a broken install. Run `:checkhealth rtlbuddy` in nvim to verify hub, language-server, and wave integration.

## Navigate and annotate signals

In Surfer, select a signal to establish the active instance scope, then choose **Go to declaration**. RTL Buddy opens the declaration and annotates signals in that scope with their values at the current waveform cursor. Moving the cursor refreshes the annotations.

To annotate only the selected signal:

```bash
rb wave basic --focused-signal
```

With `ctrl-sock` configured, place the nvim cursor on a signal and press `<leader>wa` to add it to Surfer. Select a Surfer signal first so the active scope is unambiguous.

If an nvim socket is stale, the next navigation request starts a new editor instance. If the plugin is missing, `rb wave` warns and continues without annotations.

## Open a formal counterexample

```bash
uv run rb wave-fpv demo_fpv_counter_safety
```

`rb wave-fpv` reads `fpv.yaml`, finds the first counterexample trace under the verification's artefacts, and opens it in the configured Surfer entry. Use `-c` for another config or `--surfer <name>` to override routing.

This command does not enable the editor annotation round trip, so mainline Surfer is sufficient. It fails with a clear message when the verification has not run, passed without a counterexample, or produced no trace.

## Use correct time units

Signal reads through pywellen use waveform timescale ticks. Convert them with `Waveform.hierarchy.timescale()`.

Hub and WCP navigation commands use femtoseconds. For example, with a 10 ps waveform tick, 95 ns is 9,500 ticks but 95,000,000 fs. Pass femtoseconds to `rb hub send wave-cursor` and `wave-zoom`.

Surfer command files use waveform ticks, not femtoseconds. Do not pass values between these interfaces without conversion.

## Use the coordination hub

When [the hub](hub.md) is running, `rb wave` connects automatically and shares cursor, scope, selection, and waveform values with other peers. It also accepts hub requests to navigate and curate displayed signals. Without a reachable hub, waveform viewing and editor annotation continue standalone.
