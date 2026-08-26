## Installing KLayout (optional)

KLayout is only required when streaming out GDS with `--gds` or rendering
PNGs with `--png`. The basic P&R flow works without it.

```bash
brew install --cask klayout            # macOS
# or download from https://klayout.de
```

`rb pnr` resolves `klayout` from `PATH`. If KLayout is not present,
`--gds`/`--png` logs `pnr.no_klayout` and skips streamout/render
without failing the run.
