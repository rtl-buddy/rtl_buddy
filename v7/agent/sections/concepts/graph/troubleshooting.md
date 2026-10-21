## Troubleshooting

Build errors:

- `N models are named 'X'`: rename one; the message lists the `models.yaml` files.
- `two or more models would be exported with the same top module`: give one a distinct `top:` or set `graph: false` on it.
- `declares graph: false, but its previous design-tier export could not be removed`: fix the permissions on the named directory or delete it, then rerun.
- `refusing to retract the export of model 'X'`: `artefacts/graph/design/<model>` resolves outside the design directory. Fix the model name or remove the symlink.

Build warnings (the build continues):

- `graph_build.design_export_failed`, `tb_export_failed`, `run_export_failed`: `rtl-buddy-view graph` failed. The event names its log; fix the elaboration error and rebuild.
- `graph_config.regression_load_failed`, `suite_load_failed`: a regression or `tests.yaml` file did not load, so its tests are missing. Fix the file.
- `graph_build.extract_failed`, `extract_merge_mismatch`: the external binding tier failed or disagrees with the built-in bindings; the built-in result is written. `graph-meta.json` lists examples.
- `graph_build.tb_id_collision`: testbench ids from different suites collided and were qualified with `@<suite dir>`. Rename the duplicated module.
- `graph_config.node_id_conflict`, `graph_merge.node_type_conflict`: one node id was claimed with two types and the first is kept. Rename one node so the id is unique.
- `graph_bind.cocotb_module_not_found`: the test still binds to the DUT, but nothing was scanned for `dut.<signal>` accesses or golden-model imports. Create or fix the named cocotb module file.
- `graph_bind.dpi_symbol_not_found`: the function node stays in the graph with no `implemented_by` edge. Put the C, C++ or Python DPI source under `verif/` or `spec/`.

Query and overlay:

- `no graph at <path>; run rb graph build first`: build the graph.
- `is not valid JSON` or `is not node-link JSON`: run `rb graph build --force`.
- `'X' matches N nodes; use a full node id`, `no node matches 'X'`: pick from the listed candidate ids.
- `graph_results.overlay_rejected`: the overlay has the wrong type or schema version and is ignored. Rerun `rb graph results`.
- `graph_coverage.unavailable`: the coverage source was missing or unreadable, so the join is skipped. When you named a source (`--cov-dir`, `--cov-manifest`, an `.info` file or `--coverage model`), the reason is listed under `problems`.
