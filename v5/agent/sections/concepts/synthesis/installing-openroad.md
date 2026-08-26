## Installing OpenROAD

OpenROAD is required only for the `openroad` backend. It must be built from source on macOS — no official binaries are published. See the build notes in your project's `tools/openroad/SETUP_OSX.md` (the starter template uses that filename) for the full procedure. After building, symlink the binary to a directory on `PATH`:

```bash
ln -s /path/to/OpenROAD/build/bin/openroad /usr/local/bin/openroad
openroad -version
```

The `openroad` binary must be on `PATH` when `rb synth` is invoked with `tool: "openroad"`.
