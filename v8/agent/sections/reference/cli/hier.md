## hier

```text
Usage: rtl-buddy hier [OPTIONS] NAME

 render module hierarchy via rtl-buddy-view

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    name      TEXT  with --view dut (default): model name from models.yaml; with    │
│                      --view tb: test name from tests.yaml (the test pins both the    │
│                      model + the testbench top)                                      │
│                      [required]                                                      │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --model-config     -c      TEXT  models.yaml to use [default: models.yaml]           │
│ --test-config              TEXT  tests.yaml to use (--view tb) [default: tests.yaml] │
│ --view                     TEXT  what to render: 'dut' (default) renders the model   │
│                                  hierarchy rooted at --top; 'tb' renders the         │
│                                  testbench hierarchy with the DUT called out as a    │
│                                  subtree. With --view tb the positional argument is  │
│                                  a test name.                                        │
│                                  [default: dut]                                      │
│ --format                   TEXT  output format: tree, dot, mermaid, json             │
│                                  [default: tree]                                     │
│ --output           -o      TEXT  write renderer output to file instead of stdout     │
│ --frontend                 TEXT  parser frontend (verible|slang)                     │
│ --rdc-annotations          TEXT  reset-domain map JSON from `analysis-tool           │
│                                  --emit-reset-domain-map`                            │
│ --clock-legend                   dot format only: emit a side legend of clock colors │
│ --block-diagram                  dot format only: render sibling dataflow as a block │
│                                  diagram (cluster nesting + net-labeled edges)       │
│                                  instead of the hierarchy dump; requires             │
│                                  rtl-buddy-sch >= 0.8.0                              │
│ --params                   TEXT  dot and mermaid only: which parameter overrides     │
│                                  each instance box shows: 'all' (renderer default,   │
│                                  narrowed by `// rbsch: params=` pragmas) or 'none'  │
│                                  (no parameter rows, overriding the pragmas);        │
│                                  requires rtl-buddy-sch >= 0.12.0                    │
│ --prune-flags                    with --block-diagram: drop root-port flags that no  │
│                                  edge lands on, typically clock and reset ports;     │
│                                  requires rtl-buddy-sch >= 0.12.0                    │
│ --clock-colors             TEXT  dot and mermaid only, with a clock-domain map:      │
│                                  clock to colour map JSON, {"schema_version": "1.0", │
│                                  "clocks": {"<clock>": "<swatch name or #rrggbb>"}}; │
│                                  requires rtl-buddy-sch >= 0.12.0                    │
│ --tool                     TEXT  path to the rtl-buddy-view binary                   │
│                                  [default: rtl-buddy-view]                           │
│ --help                           Show this message and exit.                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
