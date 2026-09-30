## Configure Surfer and the editor

Add a named `cfg-surfer` entry to `root_config.yaml` and route it from the active platform:

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

- `%f` and `%l` in `editor-cmd` expand to the source file and line.
- `wcp-port: 0` lets the OS pick a free port.
- `editor-sock` enables nvim reuse and annotations.
- `ctrl-sock` enables editor-to-Surfer actions.

With another editor, omit both sockets to get one-way source navigation. See [YAML Formats](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#root_configyaml) for all fields.
