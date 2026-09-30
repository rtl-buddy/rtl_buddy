## Browse the model in the hub

Start the browser layer and open `/phy`:

```bash
rb hub start --serve-viewer
```

`GET /phy.json` returns the `rb phys summary` payload with no row limit plus the `rb phys runs` listing, so the pane and the CLI agree. `GET /phy.json?dir=<phys dir>` selects a run.

- **Run dropdown.** Entries read `run · top · backends · mode (activity) · experiment`. The newest run is the default; choose the newest entry to return to following it. Hover shows the artefact directory and fingerprint.
- **Tables.** The pane ranks modules by cells or area and instances by leakage, dynamic or total power, and filters the instance table to a module when you click it. If the clicked name is an RTL module, nothing matches and the pane says so; if it is in both namespaces, the pane prints the collision note.
- **Large tables.** The instance table shows 500 rows at a time (`show more`, `show all`). Sorting, filtering and totals cover the whole set.
- **Totals.** `dynamic` is internal plus switching power, summed in the browser. The header shows the flow's own scraped total beside the sum of the rows and says when they disagree.

See [Hub](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/hub/#synthpower-pane) for the routes and the peer contract.
