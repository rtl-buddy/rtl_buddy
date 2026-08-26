## Structure

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

cfg-rtl-reg:
  reg-cfg-path: "design/regression.yaml"
```
