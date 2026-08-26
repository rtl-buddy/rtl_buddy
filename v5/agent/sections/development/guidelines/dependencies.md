## Dependencies

Classify every dependency using the buckets in [Installation](https://rtl-buddy.github.io/rtl_buddy/v5/install/#dependency-types): required dependency, integrated tool, pluggable, or pluggable curated.
Use the classification to decide whether the dependency belongs in `pyproject.toml`, user install instructions, tool manifests, root-config schema, or command-specific docs.

Every new feature or dependency must update `docs/install.md` in the same PR.
The install page is the source of truth for feature-to-dependency mapping, required external tools, optional sub-dependencies, curated tools, and fork requirements.

Every external tool dependency must also be represented in `src/rtl_buddy/tool_manifest.py` unless there is a documented reason it cannot be checked.
The manifest is the source used by `rb tool-check`, `rb tool-check --required-for`, `rb tool-check --explain`, and runtime `tool_manifest.require()` errors.
Keep the manifest's `used_by`, `optional`, `minimum_version`, detector, install hint, and notes fields aligned with `docs/install.md`.

When a new feature adds or changes tool requirements, update `tests/test_tool_manifest.py` so `rb tool-check` reports the right readiness and install guidance.
Update [Tool Dependency Check](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/tool-check/) when manifest semantics, detector behavior, exit-code behavior, or command coverage changes.

Required Python dependencies should be kept minimal because they are installed for every user.
Prefer optional external tools for feature-specific functionality unless the dependency is needed by the core CLI, config loading, local docs access, or a command that cannot operate without it.

When adding an external tool integration, document:

- the command or feature that needs it;
- whether it is integrated, pluggable, or pluggable curated;
- any required version or fork;
- optional sub-dependencies such as coverage, rendering, or notebook extras;
- the concept page that explains build or setup details.
- the `rb tool-check --explain <tool>` hint users should see when it is missing.
