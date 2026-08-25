---
description: Authoring and review guidelines for the bundled rtl_buddy skill family, including content boundaries, routing, and installer coverage.
---

# Bundled Skill Guidelines

These guidelines apply to the primary `src/rtl_buddy/skill/SKILL.md`, specialist
`src/rtl_buddy/skill/<name>/SKILL.md` files, and the installer that distributes
them. The family is operational guidance, not a second documentation site.

## Family Rules

Every bundled skill must:

- Stay under 8 KiB.
- Match its directory name in frontmatter.
- Have a unique description that discriminates it from other family members
  during automatic selection.
- Require `rb --version` at the top of every run summary.
- Route option lists, schemas, and how-tos to local `rb docs show <page>`
  references.

Do not restate schemas, field references, option lists, or worked how-tos from
`docs/reference/`. Brief operational orientation is appropriate only when an
agent needs it to act correctly before opening the docs.

## Primary Skill

The primary should explain the purpose, use case, and basic valid command for
each major feature. It should cover `--machine`, result interpretation, YAML
orientation, working-directory and output anchoring, and specialist routing.
An agent should be able to start ordinary work correctly without loading a
specialist.

## Specialist Skills

Each specialist should be self-contained when selected without the primary,
contain only non-obvious decisions and gotchas for its topic, and avoid
repeating the primary's general YAML, working-directory, and result guidance.

Do not add a specialist merely because `rtl_buddy` gained a command. Add one
only when distinct agent behavior would otherwise be wrong.

## Source And Packaging

`src/rtl_buddy/skill/SKILL.md` is the primary overview. Specialist sources live
at `src/rtl_buddy/skill/<skill-name>/SKILL.md`; installation places every family
member as a sibling under the target platform's `skills/` directory. There is
no separate source-of-truth skill repository.

Files under `src/rtl_buddy/skill/` are included in the wheel automatically by
hatchling. The docs ship through the `src/rtl_buddy/docs` symlink and the
`force-include` configuration in `pyproject.toml`, so local `rb docs ...`
references match the installed version. Keep the docs excluded from package
discovery to avoid double inclusion.

`src/rtl_buddy/skill/gitignore_snippet.txt` is the source printed by
project-level installs and `rtl-buddy skill print-gitignore`.

## Installation Contract

Skill changes reach an existing installation only after the user reruns
`rtl-buddy skill install`. `rtl-buddy skill status` reports every family member
and detects stale content through its `.rtl_buddy_skill_version` marker.

Every installed directory name must equal its `name:` frontmatter.
`SKILL_DIRNAME = "rtl-buddy"` remains the backward-compatible primary, while
`LEGACY_SKILL_DIRNAME` (`rtl_buddy`) is removed on install, reported by status,
and cleaned by uninstall.

User-level installation is the default at
`~/.claude/skills/<family-member>/` and
`~/.codex/skills/<family-member>/`. `--project` or `--root PATH` opts into
project-level siblings under `.claude/skills/` and `.agents/skills/`; those
copies override user-level members for projects pinned to a divergent major.
Do not change the default scope without deliberately revisiting this policy.

Keep `SKILL_DIRNAMES`, packaged files, install, status, uninstall, version
markers, gitignore output, and legacy migration consistent across the family.
When family membership changes, update the install, status, and uninstall tests
that guard those contracts.

## Review The Skill Family

1. Enumerate every bundled skill from `SKILL_DIRNAMES` and check the 8-KiB
   limit plus directory/frontmatter name equality.
2. Check that descriptions are unique and discriminating enough for automatic
   selection.
3. Verify every section against the family, primary, and specialist rules
   above.
4. Compare described commands with `uv run rb --help` and relevant command
   help.
5. Verify installer lifecycle tests whenever family membership changes.

For a full review, list findings under **Trim** and **Missing**. Each item should
give a one-line reason and a recommended fix. Follow
[Code Reviews](reviews.md) for pull request scope and feedback rules.
