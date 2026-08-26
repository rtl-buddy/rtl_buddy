## Installing OpenROAD

`openroad` must be on `PATH`, or its absolute path must be configured
via a `cfg-pnr-tools` entry in `root_config.yaml` (see
[yaml.md](https://rtl-buddy.github.io/rtl_buddy/v5/reference/yaml/#root_configyaml)). Build from source (no
official macOS binaries):

```bash
ln -s /path/to/OpenROAD/build/bin/openroad /usr/local/bin/openroad
```

See `tools/openroad/BUILD_OSX.md` in the project template for the macOS recipe.

### Version expectations

`rb pnr` probes `openroad -version` and compares it against an internal
`MIN_OPENROAD_VERSION` (currently `25Q1`). Older builds may still work for
the basic flow but are unvalidated — a `pnr.openroad_version_below_min`
warning is emitted and the run continues. The version is also logged at
INFO as `pnr.openroad_version` so it shows up in `rtl_buddy.log`.
