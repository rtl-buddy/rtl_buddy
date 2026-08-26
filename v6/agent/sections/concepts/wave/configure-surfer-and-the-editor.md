## Configure Surfer and the editor

Add a named entry to `root_config.yaml` and route it from the active platform:

```yaml
cfg-platforms:
  - os: osx
    unames: [Darwin]
    builder: verilator
    surfer: surfer-default

cfg-surfer:
  - name: surfer-default
    path: ../surfer/target/release/surfer
    wcp-port: 0
    editor-cmd: nvim +%l %f
    editor-terminal: tmux
    editor-sock: ~/.local/share/rtl-buddy/wave-nvim.sock
    ctrl-sock: ~/.local/share/rtl-buddy/wave-ctrl.sock
```

`%f` and `%l` expand to the source file and line. `wcp-port: 0` lets the OS select a free port. `editor-sock` enables nvim reuse and annotations; `ctrl-sock` enables editor-to-Surfer actions. Omit the sockets when using another editor for one-way source navigation.

See [YAML Formats](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#root_configyaml) for all fields.
