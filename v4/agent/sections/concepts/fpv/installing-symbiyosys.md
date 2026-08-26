## Installing SymbiYosys

`sby` must be on `PATH`, or its absolute path must be configured via a `cfg-fpv-tools` entry in `root_config.yaml` (see [yaml.md](https://rtl-buddy.github.io/rtl_buddy/v4/reference/yaml/#root_configyaml)). The standalone install is small and runs on top of the Yosys you already have for `rb synth` / `rb cdc`:

```bash
git clone https://github.com/YosysHQ/sby.git
cd sby
make install PREFIX=$HOME/.local      # no sudo
pip install click                     # sby's only Python dep
```

`sby` shells out to one or more solvers — install at least one:

```bash
brew install yices2                   # macOS — most common default
brew install z3                       # widely useful
brew install boolector                # bitvector-heavy proofs
brew install berkeley-abc             # for the `abc pdr` engine
```

For a turnkey alternative, the [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build/releases) bundles Yosys, `sby`, and every supported solver in one tarball.

### Version expectations

`rb fpv` probes `sby --version` and logs it as `fpv.sby_version` so the version is captured in `rtl_buddy.log`. There is no hard minimum today — anything 0.40 or newer should work.
