## graph build

```text
Usage: rtl-buddy graph build [OPTIONS]

 extract every tier and merge them into artefacts/graph/graph.json

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --model                                               TEXT  model name to export in  │
│                                                             the design tier;         │
│                                                             repeatable. Default:     │
│                                                             every model declared     │
│                                                             under --design-dir       │
│ --regression           -c                             TEXT  regression.yaml whose    │
│                                                             suites pin the models to │
│                                                             export (mutually         │
│                                                             exclusive with --model)  │
│ --spec-dir                                            TEXT  directory searched for   │
│                                                             specs.yaml               │
│ --verif-dir                                           TEXT  directory searched for   │
│                                                             tests.yaml               │
│ --design-dir                                          TEXT  directory searched for   │
│                                                             models.yaml              │
│ --out-dir              -o                             TEXT  output directory         │
│                                                             (default: <project       │
│                                                             root>/artefacts/graph)   │
│ --frontend                                            TEXT  viewer parser frontend   │
│                                                             (verible|slang)          │
│ --design                   --no-design                      run the rtl-buddy-view   │
│                                                             design tier (default on) │
│                                                             [default: design]        │
│ --tb                       --no-tb                          also export each         │
│                                                             testbench's own          │
│                                                             hierarchy, rooted at its │
│                                                             toplevel: (default on;   │
│                                                             --no-tb is DUT-only)     │
│                                                             [default: tb]            │
│ --flow-tops                --no-flow-tops                   also export each         │
│                                                             formal/synth/cdc run's   │
│                                                             top over the flow's own  │
│                                                             filelist when it is not  │
│                                                             the model top (default   │
│                                                             on)                      │
│                                                             [default: flow-tops]     │
│ --bind                     --no-bind                        run the post-merge       │
│                                                             binding stage that ties  │
│                                                             cocotb tests to the DUT  │
│                                                             hierarchy (default on)   │
│                                                             [default: bind]          │
│ --extract                  --no-extract                     run the binding tier     │
│                                                             when the extractor       │
│                                                             (rtl-buddy-graph-extrac… │
│                                                             is installed             │
│                                                             [default: extract]       │
│ --extract-cross-check      --no-extract-cross-che…          cross-check the internal │
│                                                             merge against the        │
│                                                             extractor's              │
│                                                             `merge-graphs` when it   │
│                                                             is installed             │
│                                                             [default:                │
│                                                             extract-cross-check]     │
│ --force                                                     rebuild even when no     │
│                                                             input changed            │
│ --strict                                                    exit non-zero on any     │
│                                                             per-item failure, not    │
│                                                             just a dead tier         │
│ --tool                                                TEXT  path to the              │
│                                                             rtl-buddy-view binary    │
│                                                             [default:                │
│                                                             rtl-buddy-view]          │
│ --help                                                      Show this message and    │
│                                                             exit.                    │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
