## Coverage pane

Open `/cov` after a coverage-producing run. `GET /cov.json` uses the same builder as `rb cov summary`, so CLI and browser totals agree. `GET /cov/source?path=...` serves only files named by the coverage model, from under the project root.

The pane offers metric filtering, coldest-file ordering, a per-test view, annotated source, and per-point attribution. Its numbers are per elaboration; the run's source-point percentages are in the header tooltip. Clicking source opens the editor peer, and clicking a module focuses the graph pane.

Reload after a run finishes if the landing page has not updated. Collection and metric definitions are in [Coverage](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/coverage/).
