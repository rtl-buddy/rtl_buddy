## Coverage pane

Open `/cov` after a coverage-producing run. `GET /cov.json` uses the same builder as `rb cov summary`, so CLI and browser totals agree. `GET /cov/source?path=...` serves only files named by the coverage model, from under the project root.

The pane offers metric filtering, coldest-file ordering, a per-test view, annotated source, and per-point attribution. The `figures` picker switches the totals, file list and tests table between per-elaboration figures (the default) and source points, as `rb cov summary --by-source` does: same files, same order, different figures. The other figure is in the header tooltip. Open `/cov?by=source` to start on source points; the pane keeps `?by=` in the address as you switch. Clicking source opens the editor peer, and clicking a module focuses the graph pane.

Reload after a run finishes if the landing page has not updated. Collection and metric definitions are in [Coverage](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/coverage/).
