## Hub integration

The [coordination hub](https://rtl-buddy.github.io/rtl_buddy/v4/concepts/hub/) consumes `rb hier`'s JSON output (`--format json`) to drive the rtl-buddy-view SPA's interactive hierarchy view. Annotation files (`--cdc-annotations`, `--rdc-annotations`, `--overlay axi-perf=...`) are surfaced as overlays in the SPA when the hub is running.

`rb hub start --model <name>` discovers the model's `models.yaml`, invokes `rb hier` under the hood, and serves the result alongside live diagnostics and AXI-perf overlays — `rb hier` is the underlying renderer for both the static CLI use case and the live SPA flow.
