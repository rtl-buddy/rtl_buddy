---
description: Rules for rtl_buddy review scope, guideline selection, evidence, and feedback.
---

# Code Reviews

Use this page to select the rules for a change and report actionable findings once.

## Set Review Scope

Review changes introduced by the pull request and the behavior they affect. If the pull request edits a guideline, use the base-branch version of that guideline to review the edit.

Apply [Engineering Guidelines](guidelines.md) to every change. Report a violation only when changed code conflicts with a rule or contract you can cite; preferences are not defects.

## Select Additional Guidelines

Apply every matching row:

| Guideline | Apply when the change affects |
| --- | --- |
| [Documentation](docs.md) | Docs, CLI help, configuration, or user-visible behavior |
| [Bundled skills](bundled-skills.md) | `src/rtl_buddy/skill/` or the skill installer |
| [Project template](project-template.md) | Public CLI, configuration, workflow, plugin, or verdict behavior |

If the project template is unavailable, report the required downstream check instead of claiming it passed.

## Report Findings

Normal pull request findings must be consequential, actionable, introduced by the change, and tied to an exact rule or contract. Put line-specific findings inline when possible. Keep summary feedback at the top level and do not repeat the same finding in both places.

For an explicitly requested full audit, use the report format in the applicable guideline. A full audit may include pre-existing gaps; a normal pull request review may not.
