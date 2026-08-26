## Out of scope (today)

- **Non-AXI protocols.** AXI4 / AXI4-Lite / AXI4-Stream are supported by `rtl-buddy-axi-profiler`'s bundle discovery; AHB, APB, TileLink, and custom protocols are not. The pluggable wrapper boundary makes adding sibling profilers straightforward, but no other profilers are wired up yet.
- **Background-detached `notebook`.** `--daemon` is accepted for forward compatibility but runs in foreground today.
- **Manifest user-edit merging.** `rb axi-profile discover --amend <prev>` is reserved for merging user edits across re-runs; today the manifest is rewritten in full and you diff in git.
