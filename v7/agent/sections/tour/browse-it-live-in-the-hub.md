## Browse it live in the hub

`rb hub` is a small daemon that serves four browser panes over the artefacts the sections above produced, and keeps them, Surfer, and your editor pointed at the same thing. One client per pane; a click in any of them selects in the others.

```bash
uv run rb graph build
uv run rb hub start --serve-viewer --model demo_tiny_alu_subsys_top \
    --models-file design/demo_tiny_alu_subsys/models.yaml
```

![The hub landing page: four cards for the schematic, graph, coverage, and area/power panes, plus the project root, active model, and graph build age](../static/img/tour/hub-index.png)

**Schematic** (`/sch`). The hierarchy from `rb hier`, drawn by [rtl-buddy-sch](https://github.com/rtl-buddy/rtl-buddy-sch) with a DUT or testbench root, an instance filter, and overlays. The coverage overlay below colours each block by line coverage from the latest run; only the ALU under `u_compute` has a coverage run behind it, so only it is green:

![Hub schematic of demo_tiny_alu_subsys_top with the coverage overlay on and the instance tree in the left panel](../static/img/tour/hub-sch.png)

**Knowledge graph** (`/gph`). The 800-node graph from `rb graph build`, laid out by tier: spec, design, test config, synthesis config, formal config, CDC config, and cocotb. Clicking a node selects it in the schematic and opens its source in the editor:

![Hub graph pane showing every tier of the design knowledge graph with per-tier node counts and the inspector panel](../static/img/tour/hub-gph.png)

**Coverage** (`/cov`). The same manifest `rb cov summary` reads, with per-test columns, per-file bars, and every cover point with its hit count. A click on a source line sets the coverage focus everywhere else:

![Hub coverage pane: five metric tiles, the four ALU tests, the four source files, and the twenty-two cover points with hit counts](../static/img/tour/hub-cov.png)

**Area and power** (`/phy`). The physical model that `rb synth` and `rb power` wrote, ranked by cells and by instance power, with the run selector at the top switching between the four power runs:

![Hub phys pane for the paired Nangate45 synthesis and static power run: cells, area, leakage and dynamic tiles, modules ranked by cell count, instances ranked by total power](../static/img/tour/hub-phy.png)

Every pane is one self-contained HTML file served from the wheel, so the hub needs no network access and no build step. `rb hub send` scripts the same selections from a terminal, and `rb mcp` exposes the hub's state to an agent. See [Hub](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/hub/).
