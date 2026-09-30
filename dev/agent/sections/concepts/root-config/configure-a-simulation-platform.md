## Configure a simulation platform

A minimal configuration maps the host `uname` to a builder:

```yaml
rtl-buddy-filetype: project_root_config

cfg-platforms:
  - os: osx
    unames: [Darwin]
    builder: verilator
    verible: verible-macos
    surfer: surfer-default

cfg-rtl-builder:
  - name: verilator
    builder: verilator
    builder-simv: obj_dir/simv
    sim-rand-seed: 31310
    sim-rand-seed-prefix: +verilator+seed+
    builder-opts:
      debug:
        compile-time: --binary -sv -o simv
        run-time: +verilator+rand+reset+2
      reg:
        compile-time: --binary -sv -o simv
        run-time: +verilator+rand+reset+2

cfg-verible:
  - name: verible-macos
    path: /opt/homebrew/bin

cfg-surfer:
  - name: surfer-default
    path: surfer

cfg-rtl-reg:
  reg-cfg-path: regression.yaml
```

- If several platform entries match `uname`, the last match wins. Routing names are validated on every platform entry at load time, including entries for other hosts.
- A platform routes simulation builders, Verible, and Surfer. It cannot route `cfg-synth-tools`, `cfg-pnr-tools`, `cfg-power-tools`, `cfg-cdc-tools`, `cfg-fpv-tools`, or `cfg-fpga-tools`; each flow's `tool:` field selects those entries directly.
- `--builder`, `--builder-mode`, or a flow-specific option overrides the platform default for one command. See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/cli/).
