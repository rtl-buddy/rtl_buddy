## Subcommand: `--explain`

```bash
rb tool-check --explain surfer
```

Prints the full manifest entry for a single tool — description, used-by subcommands, per-platform install hints, minimum version, and optional notes. Example:

```
surfer — Web-native waveform viewer
  Status:  ok
  Version: 0.3.0
  Path:    /opt/homebrew/bin/surfer
  Used by: rb wave, rb wave-fpv, rb hub
  Install:
    source   https://github.com/rtl-buddy/surfer (branch rtl-buddy)
    build    cd ../surfer && cargo build --release
```

This is also what subcommand wrappers point you at when they refuse to run because a tool is missing — e.g. `rb wave` saying "surfer not found — run `rb tool-check --explain surfer`".
