## root_config.yaml

The root config lives at the project root. It defines platforms, builders, Verible, and the default regression config path.

**Required keys:**

- `rtl-buddy-filetype: project_root_config`
- `cfg-platforms`
- `cfg-rtl-builder`
- `cfg-verible`
- `cfg-rtl-reg`

**Full example:**

```yaml
rtl-buddy-filetype: project_root_config

cfg-platforms:
  - os: "osx"
    unames: ["Darwin"]
    builder: "verilator"
    verible: "verible-macos"

cfg-rtl-builder:
  - name: "verilator"
    builder: "verilator"
    builder-simv: "obj_dir/simv"
    sim-rand-seed: 31310
    sim-rand-seed-prefix: "+verilator+seed+"
    builder-opts:
      debug:
        compile-time: "--binary -sv -o simv"
        run-time: "+verilator+rand+reset+2"
      reg:
        compile-time: "--binary -sv -o simv"
        run-time: "+verilator+rand+reset+2"

cfg-verible:
  - name: "verible-macos"
    path: "tools/verible/macos/active/bin"
    extra_args:
      lint:
        - "--rules=-module-filename"

cfg-coverage:
  - name: "verilator"
    use-lcov: true

cfg-coverview:
  - name: "verilator"
    generate-tables: "line"
    config:
      # inline Coverview JSON configuration values

cfg-rtl-reg:
  reg-cfg-path: "design/regression.yaml"
```

**Runtime effects:**

- Platform is selected by matching `uname` output against `cfg-platforms[].unames`.
- `--builder` overrides the platform-selected builder for the current run.
- `--builder-mode` selects which named `builder-opts` entry to use for compile-time and run-time flags.
- `cfg-coverage` is keyed by simulator family (e.g. `verilator`). `use-lcov: true` enables `.info` export and LCOV HTML generation when `--coverage-html` is used.
- `cfg-coverview` is keyed by simulator family. `generate-tables` sets the coverage type for Coverview tables. `config` is a dict of inline Coverview JSON configuration values.
- `cfg-rtl-reg.reg-cfg-path` is the fallback regression file for `rtl-buddy regression` when no `./regression.yaml` exists in the cwd.

---
