## Run ABC in batch mode

Yosys 0.64 on Linux keeps one `yosys-abc` process alive and sends it each ABC script over a pipe. It waits for `yosys-abc` to echo the `source <TMPDIR>/.../abc.script` line after its prompt. With `TERM` unset, readline assumes an 80-column screen and echoes a long line scrolled sideways, so the prompt never arrives and both processes block. Since rtl_buddy 6.81.0, every Yosys launch gets `COLUMNS=100000`, which keeps the echo on one line.

If a run still stops after ABC with `output.blif` written, set `abc-persistent: false`:

```yaml
cfg-synth-tools:
  - name: yosys
    tool: yosys
    opts:
      abc-persistent: false
```

- Set it in `cfg-synth-tools` `opts`, or for one run in `tool_overrides.<tool>.abc_persistent`. The run override wins.
- The run links `yosys-tmp/abc-batch` to the `yosys-abc` beside its Yosys binary, or on `PATH`, and starts `synth.ys` with `scratchpad -set abc.exe <link>`. Yosys reuses the process only for its own `yosys-abc` path, so every ABC call, including `synth`'s own, runs as `yosys-abc -s -f <script>`. The binary and the mapping are unchanged; each call pays process start-up.
- It applies to `tool: yosys` and to the Yosys stage of `tool: openroad`. A run with no `yosys-abc` to link is fatal.
- The setting joins the run's options fingerprint only when `false`.
