## Hub integration

When the [coordination hub](https://rtl-buddy.github.io/rtl_buddy/v4/concepts/hub/) is running for the current project, every successful `rb cdc` analysis publishes its violations to the hub as a `diagnostics_set` event under the source key `rb-cdc:<analysis_name>`. The rtl-buddy-view SPA's on-canvas badge layer and `rtl-buddy-nvim`'s `rtlbuddy` diagnostics namespace light up immediately — no `rb hub send` copy-paste.

The publish step is best-effort: missing hub, no live PID, connect failure, or a malformed JSON payload all silently no-op with a debug-level log line. The CDC analysis itself is never failed by a sidecar UI being unreachable.

Re-running an analysis after a fix replaces (or clears) just that source-key slot, so a project with several analyses doesn't have one fix wiping all the others.
