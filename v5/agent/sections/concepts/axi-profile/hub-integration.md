## Hub integration

When the [coordination hub](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/hub/) is running, two paths surface AXI-perf data in the rtl-buddy-view SPA:

- **Static overlay**: `rb hub start --axi-perf-from <axi-perf.json>` threads the test's `axi-perf.json` into the SPA's view builder, decorating each AXI bundle node with throughput badges. It also records the source test and suite dir so the SPA's "Open in marimo" button can launch the matching notebook without re-prompting — point `--axi-perf-from` at the canonical `<suite>/artefacts/axi/<test>/axi-perf.json` so that derivation lands.
- **Notebook launch**: the SPA's "Open in marimo" button calls `/api/axi-profile/notebook?test=<name>`, which invokes `rb axi-profile notebook <test> --headless` and proxies the marimo URL back to the SPA. The user gets the full interactive notebook without leaving the hub UI.
- **Live event sync**: a hub-launched notebook also joins the hub's event broker as a peer (`origin=notebook`) via the `RB_HUB_EVENTS_URL` environment variable the hub injects. SPA bundle-node clicks are then forwarded to the running notebook, so the deep-dive view tracks the SPA selection.

All three flows reuse the same per-test artefact layout, so the static, interactive, and live views agree on what data they're showing.
