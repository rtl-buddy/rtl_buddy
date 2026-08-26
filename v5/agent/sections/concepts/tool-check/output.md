## Output

The default text format has two sections plus a hint line:

```
Tools (12 ok, 1 missing, 1 outdated)
----------------------------------------------------------------------
Tool                  Status      Version       Path
verible               ok          v0.0-3724     /opt/homebrew/bin/verible-verilog-syntax
yosys                 ok          0.45+115      /opt/homebrew/bin/yosys
verilator             outdated    5.0.18        /opt/homebrew/bin/verilator  (need ≥ 5.020)
surfer                ok          0.3.0         /opt/homebrew/bin/surfer
sby                   missing     —             —  (optional)
...

Subcommand readiness
----------------------------------------------------------------------
  ok        rb test                 (verible, yosys, verilator, ...)
  outdated  rb regression           (outdated: verilator)
  missing   rb fpv                  (needs: sby)                            (optional feature)
  ...

Hint: `rb tool-check --explain <tool>` for install instructions.
```

The **Tools** section is the per-tool table — name, status (`ok` / `missing` / `outdated`), captured version, resolved path. Python-package detectors show `(python)` in the Path column. A `(need ≥ X)` suffix appears when a tool is present but below `minimum_version`. An `(optional)` suffix appears for tools whose absence does not gate any subcommand.

The **Subcommand readiness** section lists every `rb <subcommand>` whose deps are declared in the manifest. The gloss after each subcommand calls out what is missing or outdated, or lists the participating tools when everything is OK. `(optional feature)` indicates a subcommand whose deps are all optional — `rb wave` is ready even without `gtkwave` installed, for example.
