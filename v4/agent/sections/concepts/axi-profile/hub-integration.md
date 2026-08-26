## Hub integration

When the [coordination hub](https://rtl-buddy.github.io/rtl_buddy/v4/concepts/hub/) is running, two paths surface AXI-perf data in the rtl-buddy-view SPA:

- **Static overlay**: `rb hub start --axi-perf-from <test>` threads the test's `axi-perf.json` into the SPA's view builder, decorating each AXI bundle node with throughput badges.
- **Notebook launch**: the SPA's "Open in marimo" button calls `/api/axi-profile/notebook?test=<name>`, which invokes `rb axi-profile notebook <test> --headless` and proxies the marimo URL back to the SPA. The user gets the full interactive notebook without leaving the hub UI.

Both flows reuse the same per-test artefact layout, so the static and interactive views agree on what data they're showing.
