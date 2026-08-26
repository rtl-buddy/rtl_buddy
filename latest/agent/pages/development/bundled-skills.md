---
description: Authoring and review rules for the bundled rtl_buddy skill family, including content boundaries, packaging, and installer coverage.
---

# Bundled Skill Guidelines

Use these rules for `src/rtl_buddy/skill/SKILL.md`, specialist skill files, and the installer that distributes them. Skills provide agent-specific operating guidance; the docs remain the detailed reference.

## Set Content Boundaries

Every skill must:

- Stay under 8 KiB.
- Match its directory name in frontmatter.
- Have a description that distinguishes it from every other family member.
- Require `rb --version` at the top of each run summary.
- Link option lists, schemas, and procedures through `rb docs show <page>` instead of copying them.

The primary skill covers the minimum needed for ordinary work: feature selection, basic commands, `--machine`, result interpretation, YAML orientation, path anchoring, and specialist routing.

A specialist must work when selected alone and contain only non-obvious decisions and failure modes for its topic. Do not repeat general guidance from the primary or create a specialist solely because a command exists.

## Preserve Packaging And Installation Contracts

The primary source is `src/rtl_buddy/skill/SKILL.md`; specialists live at `src/rtl_buddy/skill/<skill-name>/SKILL.md`. Hatchling includes that tree in the wheel. Installed family members are sibling directories under the selected platform's `skills/` directory.

The wheel also includes the docs through `src/rtl_buddy/docs` and `pyproject.toml` `force-include`, so `rb docs` matches the installed version. Keep docs excluded from package discovery to avoid packaging them twice.

Installation uses these contracts:

- `SKILL_DIRNAMES` lists every family member.
- Each installed directory matches its skill `name:`.
- `rtl-buddy skill install` refreshes the family; `status` compares `.rtl_buddy_skill_version`; `uninstall` removes managed copies.
- `src/rtl_buddy/skill/gitignore_snippet.txt` supplies project-install and `print-gitignore` output.
- User scope is the default. `--project` and `--root PATH` create project copies that override user copies.
- Install, status, uninstall, version markers, gitignore handling, and managed obsolete-directory cleanup cover the same family membership.

Do not change the default scope without an explicit policy decision. When membership changes, update the lifecycle tests with the source files and constants.

## Review A Skill Change

1. Enumerate the family from `SKILL_DIRNAMES`.
2. Check size, directory/frontmatter names, and description uniqueness.
3. Apply the primary or specialist content boundary above.
4. Verify commands against `uv run rb --help` and relevant subcommand help.
5. Verify packaging and installer lifecycle tests when files or membership change.

For a full audit, report findings under **Trim** and **Missing**, with one reason and one action per item. Apply [Code Reviews](reviews.md) to pull request scope and feedback.
