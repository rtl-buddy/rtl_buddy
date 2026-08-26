## Determine Required Coverage

Compare user-visible changes since the template's last `rtl_buddy` dependency bump, or use the requested change range for a targeted review. Check:

- CLI commands and options in `src/rtl_buddy/rtl_buddy.py` and `docs/reference/cli.md`.
- Configuration in `src/rtl_buddy/config/` and `docs/reference/yaml.md`.
- Workflows and plugin behavior in `docs/concepts/`.
- Pass/fail behavior in `src/rtl_buddy/tools/`.

Internal refactors with no behavior or configuration change need no template update. For each relevant behavior, locate its configuration, RTL, plugin, and explanatory material in the template, then verify the role and usability rules above.

For a full audit, report:

| Feature / change | `rtl_buddy` commit | Role | Template location | Isolated | Integrated/minimal | Explained | Runnable | Action |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

Use pass, fail, or warning values and follow the table with one action for every non-pass row. Apply [Code Reviews](https://rtl-buddy.github.io/rtl_buddy/v6/development/reviews/) to pull request scope and feedback. If the template is unavailable, report the required downstream check instead of marking it complete.
