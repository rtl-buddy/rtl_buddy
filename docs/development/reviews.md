---
description: Authoritative rtl_buddy code-review scope, guideline routing, evidence standards, and feedback rules.
---

# Code Reviews

This page is the single source of truth for reviewing `rtl_buddy` changes. It
routes each change to the applicable domain guidelines while keeping shared
review behavior in one place.

## Review Scope

Review only changes introduced by the pull request and the behavior they
affect. When the pull request edits a guideline or review page, use the
base-branch version as the rule for reviewing that change.

Check every change against [Engineering Guidelines](guidelines.md). Report a
guideline violation only when the changed code conflicts with a rule you can
cite; do not promote generic preferences into defects.

## Select Guidelines

Apply every row whose scope matches the change:

| Guideline | Apply when the change affects |
|---|---|
| [Documentation](docs.md) | Documentation, CLI help, configuration fields, or other user-visible behavior. |
| [Bundled skills](bundled-skills.md) | A file under `src/rtl_buddy/skill/` or the skill installer. |
| [Project template](project-template.md) | Public CLI, configuration, workflow, plugin, or pass/fail behavior. |

If the project template is unavailable, identify the expected downstream check
as a follow-up instead of claiming that the downstream review passed.

## Report Findings

Keep normal pull request findings consequential, actionable, and limited to
defects or gaps introduced by the change. Cite the exact rule or contract that
the change violates.

Put a line-specific finding inline when the review channel supports it. Keep
summary feedback at the top level and do not duplicate a finding across both
locations.

For an explicitly requested full review, use the report format defined by the
matching guideline page. A full review may report pre-existing gaps; a normal
pull request review may not.
