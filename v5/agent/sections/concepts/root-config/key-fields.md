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

Defines Verible tool configurations for lint and syntax checks. `path` is the directory containing Verible executables — absolute or relative to `root_config.yaml`.

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
rb wave-install-nvim          # installs rtl_buddy_wave.lua to ~/.local/share/nvim/site/plugin/
rb wave-install-nvim --force  # overwrite an existing installation
```

The plugin provides the `WaveValue` highlight group and a `VimEnter` hook required for annotation to work.

**`cfg-rtl-reg`**

Sets the default path to `regression.yaml` used by `rtl-buddy regression` when `--reg-config` is not specified.
