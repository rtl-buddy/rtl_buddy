## Hub integration

The [coordination hub](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/hub/) consumes `rb hier`'s JSON output (`--format json`) to drive the rtl-buddy-view SPA's interactive hierarchy view. The `rb hier` clock/reset overlays (`--cdc-annotations`, `--rdc-annotations`) are real CLI flags and are surfaced as overlays in the SPA. The AXI-perf overlay is **not** a `rb hier` flag — it is baked into the SPA view only via `rb hub start --axi-perf-from <axi-perf.json>` (see [Hub](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/hub/#axi-perf-overlay-and-notebook-spawning)), which invokes the renderer with `--overlay axi-perf=<path>` internally.

`rb hub start --model <name>` discovers the model's `models.yaml`, invokes `rb hier` under the hood, and serves the result alongside live diagnostics and AXI-perf overlays — `rb hier` is the underlying renderer for both the static CLI use case and the live SPA flow.
