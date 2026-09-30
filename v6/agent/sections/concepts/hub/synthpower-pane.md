## Synth+power pane

Open `/phy` after `rb synth` or `rb power`. `GET /phy.json` uses the same builder as `rb phys summary`, so numbers agree with the CLI, but it never truncates: the pane sorts and filters the whole model in the browser. It returns 404 with a command hint when the project has no `phys-manifest.json`. Model and CLI verbs are in [Physical Metrics](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/phys/).

The metrics are `cells`, `area`, `leakage`, `dynamic` and `total`. `dynamic` is internal plus switching. The totals header shows the flow's own total beside the sum of the rows and flags a disagreement instead of reconciling it.

A model with only one half still works. The pane names the command that fills the other half. If the power half came from a routed database (`netlist-source: pnr`), it cannot merge with a synthesis, so the banner tells you to synthesise first and rerun `rb power` on the new netlist.

Clicking a module focuses the graph pane, and clicking an instance selects it in the schematic. An inbound selection highlights the matching instance row.

### Select a run

The run dropdown in the pane header lists the 50 newest runs. `/phy.json` follows the newest run; `/phy.json?dir=<project-relative phys_dir>` selects another.

- A `dir` outside the project root returns 403, and a directory with no `phys-manifest.json` returns 404 and names `rb phys runs`.
- A refused selection keeps the current run on screen and reports the refusal in the status line.
