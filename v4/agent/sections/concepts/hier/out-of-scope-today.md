## Out of scope (today)

- **rtl-buddy-view only.** No alternative hierarchy renderers are wired up. The integration is intentionally subprocess-granularity so a viewer release can be picked up via `uv sync` without code changes here.
- **In-place SVG/PNG.** `rb hier` does not directly emit SVG or PNG — it emits DOT and lets you pipe through Graphviz. This keeps the rtl_buddy ↔ renderer boundary at "text in, text out" and avoids a Graphviz dependency for the common terminal-inspection flow.
