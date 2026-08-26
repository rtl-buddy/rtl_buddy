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
│ --cdc-annotations          TEXT  clock-domain map JSON from `rtl-buddy-cdc           │
│                                  --emit-domain-map`                                  │
│ --rdc-annotations          TEXT  reset-domain map JSON from `rtl-buddy-cdc           │
│                                  --emit-reset-domain-map`                            │
│ --clock-legend                   dot format only: emit a side legend of clock colors │
│ --tool                     TEXT  path to the rtl-buddy-view binary                   │
│                                  [default: rtl-buddy-view]                           │
│ --help                           Show this message and exit.                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
