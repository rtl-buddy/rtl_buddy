## Focus the pane from another app

Point the pane at a target from anywhere:

```bash
rb hub send phys-focus module:sub --metric area
rb hub send phys-focus instance:u_sub/_64_
```

- An unprefixed target is an instance path. An instance target must name a leaf row; a subtree path (`match: prefix`) selects nothing, so send one of its children.
- The message names a target and a metric, never a run, so it applies to the run the pane shows. The hub replays the latest focus when the pane opens.
- Clicking a module broadcasts `graph_focus` and clicking an instance broadcasts `selection_changed`, which the schematic follows.

The schematic and the pane share paths only when both show the same design. A `/sch` showing a testbench around the DUT, or another design, selects nothing. Open the schematic on the synthesis `top:`. See [Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#phys-pane-and-schematic-selections-cross-only-within-one-hierarchy).
