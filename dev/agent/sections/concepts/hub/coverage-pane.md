## Coverage pane

Open `/cov` after a coverage-producing run. `GET /cov.json` uses the same coverage-model builder as `rb cov summary`, so CLI and browser totals agree. `GET /cov/source?path=...` serves only files named by that coverage model and only from under the project root.

The pane supports metric filtering, coldest-file ordering, a per-test lens, annotated source, and per-point attribution. Clicking source can send `source_focused` and `open_source`; clicking a module can send `graph_focus`. The hub resolves source locations into schematic selections where possible.

Coverage discovery is cached briefly. Reload after a run finishes if the landing page has not yet updated. See [Coverage](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/coverage/) for collection and metric definitions.
