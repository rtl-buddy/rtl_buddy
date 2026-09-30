## Install the tools

OpenROAD 25Q1 or newer must be on `PATH` or set in `cfg-pnr-tools`. An older version logs a warning and the run continues, but that combination is not validated. On macOS, build from source with the template's `tools/openroad/BUILD_OSX.md`.

KLayout (`brew install --cask klayout`) is optional and only needed for `--gds` and `--png`. Without it those steps are skipped and the run does not fail. Install it later and [export the saved result](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#export-a-saved-result) instead of rerunning P&R.
