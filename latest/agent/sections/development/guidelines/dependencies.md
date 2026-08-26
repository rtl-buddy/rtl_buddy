## Dependencies

Classify dependencies as required, integrated, pluggable, or pluggable curated; see [Installation](https://rtl-buddy.github.io/rtl_buddy/v6/install/#dependency-types). Keep required Python dependencies minimal and use optional tools for feature-specific functionality.

For every external tool, update `docs/install.md`, `src/rtl_buddy/tool_manifest.py`, and `tests/test_tool_manifest.py` together. Keep `used_by`, optional status, minimum version, detector, install hint, and notes aligned. Document:

- the command or feature that needs it;
- whether it is integrated, pluggable, or pluggable curated;
- any required version or fork;
- optional sub-dependencies such as coverage, rendering, or notebook extras;
- the concept page that explains build or setup details.
- the `rb tool-check --explain <tool>` recovery hint.

Update [Tool Dependency Check](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tool-check/) when manifest behavior changes.
