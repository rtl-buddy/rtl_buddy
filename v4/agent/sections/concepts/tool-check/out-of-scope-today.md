## Out of scope (today)

- **Tool installation.** `rb tool-check` reports state and gives install hints; it does not run installers itself. Treat the install hints as documentation, not as automation.
- **Cross-platform install scripts.** Hints are per-OS (`macos`, `linux`, `source`, `vendor`, `any`); a unified setup script generator is a future possibility but not built today.
- **Custom user manifests.** The manifest is built-in; projects can pin versions and binary paths via `root_config.yaml`, but they cannot add wholly new tools to the manifest. Adding a tool is a code change to `src/rtl_buddy/tool_manifest.py`.
