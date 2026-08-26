## Configuration

Add a `cfg-surfer` section to `root_config.yaml`:

```yaml
cfg-surfer:
  - name: "surfer-default"
    path: "../surfer/target/release/surfer"  # or bare name on PATH
    wcp-port: 0                              # 0 = OS assigns a free port
    editor-cmd: "nvim +%l %f"               # %f = file, %l = line
    editor-terminal: "tmux"                  # tmux | iterm2 | terminal | ""
    editor-sock: "~/.local/share/rtl-buddy/wave-nvim.sock"  # enables nvim reuse
    ctrl-sock: "~/.local/share/rtl-buddy/wave-ctrl.sock"    # enables nvim → Surfer
```

See [YAML Formats](https://rtl-buddy.github.io/rtl_buddy/v4/reference/yaml/) for all fields.
