## Install the tools

OpenROAD 25Q1 or newer must be on `PATH` or configured in `cfg-pnr-tools`. RTL Buddy warns and continues with an older version, but that combination is not validated.

On macOS, use the project template's `tools/openroad/BUILD_OSX.md` source-build instructions.

KLayout is optional and used only for `--gds` and `--png`:

```bash
brew install --cask klayout
```

A missing KLayout skips GDS or PNG generation without failing the OpenROAD run.
