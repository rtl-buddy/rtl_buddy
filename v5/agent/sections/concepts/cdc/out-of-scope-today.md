## Out of scope (today)

- **rtl-buddy-cdc only.** Commercial CDC tools (SpyGlass CDC, JasperGold CDC, Questa CDC) are not yet wired up — adding them follows the same pattern documented for `rb fpv`'s SymbiYosys backend.
- **Reset-domain crossing (RDC).** Reset-domain analysis is a planned extension of `rtl-buddy-cdc`; once it lands there, `rb cdc` will surface its findings alongside CDC. Today RDC overlays surface in `rb hier --rdc-annotations` via a separate analyzer pass.
