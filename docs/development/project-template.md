---
description: Rules for keeping rtl-buddy-project-template aligned with user-visible rtl_buddy behavior through focused, runnable examples.
---

# Project Template Guidelines

Use these rules when adding examples to [`rtl-buddy-project-template`](https://github.com/rtl-buddy/rtl-buddy-project-template) or checking it against `rtl_buddy`.

## Choose The Example Role

| Role | Use it for | Boundary |
| --- | --- | --- |
| Sandbox flow | Behavior that belongs in the integrated reference design | Extend its DUT, spec, model, test, and regression story. |
| Template demo | One capability best shown in isolation | Keep it small, separate, and easy to copy. |

Do not add a disconnected demo to the sandbox or let a template demo become an alternate integrated project.

## Make The Example Usable

Every example must be:

- Easy to locate and understand without unrelated material.
- Integrated with the sandbox or deliberately minimal.
- Accompanied by a short explanation of when to use it.
- Runnable with a concrete `uv run rb ...` command.

An RTL engineer new to `rtl_buddy` should be able to run and adapt it without reconstructing missing context.

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

Use pass, fail, or warning values and follow the table with one action for every non-pass row. Apply [Code Reviews](reviews.md) to pull request scope and feedback. If the template is unavailable, report the required downstream check instead of marking it complete.
