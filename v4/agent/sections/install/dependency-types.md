## Dependency types

rtl_buddy classifies dependencies into four buckets:

- **Required dependency**: Installed automatically with the `rtl_buddy` wheel; no external setup.
- **Integrated tool**: A rtl_buddy feature is built around one specific tool; you must install that exact tool to use the feature with no alternatives supported.
- **Pluggable**: rtl_buddy defines an interface; any tool that fits the interface works. rtl_buddy does not know what the tool specifically is or does — it just hands it the inputs the interface promises and consumes the outputs the interface promises.
- **Pluggable, curated**: tools that plug into the same plug point as **Pluggable**, but rtl_buddy carries first-class optimizations triggered by the tool name (e.g. coverage merging tuned for a specific simulator, a two-stage flow when a specific synthesis backend is selected). Having curated tools does not prevent non-curated tools from plugging into the same plug points.
