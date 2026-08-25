---
description: How to audit the bundled rtl_buddy skill family for lean, accurate guidance, correct routing, and installer coverage.
---

# Audit The Bundled Skill Family

This audit covers the primary `src/rtl_buddy/skill/SKILL.md`, specialist
`src/rtl_buddy/skill/<name>/SKILL.md` files, and the installer that distributes
them. The family is operational guidance, not a second documentation site: the
primary covers basic use and routing, while specialists contain only
non-obvious guidance for their topics.

## Design Principles

Every bundled skill should:

- Stay under 8 KiB.
- Match its directory name in frontmatter.
- Have a unique description that discriminates it from the other family
  members during automatic selection.
- Require `rb --version` at the top of every run summary.
- Route option lists, schemas, and how-tos to local `rb docs show <page>`
  references.

The primary should explain the purpose, use case, and basic valid command for
each major feature. It should cover `--machine`, result interpretation, YAML
orientation, working-directory and output anchoring, and specialist routing.
An agent should be able to start ordinary work correctly without loading a
specialist.

Each specialist should be self-contained when selected without the primary,
contain only non-obvious decisions and gotchas for its topic, and avoid
repeating the primary's general YAML, working-directory, and result guidance.

The skill should not:

- Restate schemas, field references, examples, option lists, or flag
  descriptions from `docs/reference/`.
- Add a specialist merely because `rtl_buddy` gained a command; add one only
  when distinct agent behavior would otherwise be wrong.
- Duplicate documentation beyond concise operational orientation.

## Run The Audit

1. Enumerate every bundled skill from `SKILL_DIRNAMES` and check the 8-KiB
   limit plus directory/frontmatter name equality.
2. Check that descriptions are unique and discriminating enough for automatic
   selection.
3. For each section, ask whether an agent needs it to act correctly before
   opening the docs.
4. Move reference detail, option lists, schemas, and worked how-tos to docs
   citations.
5. Compare described commands with `uv run rb --help` and relevant command
   help.
6. Check install, status, and uninstall tests whenever family membership
   changes.

## Report The Audit

For a full audit, list findings under **Trim** and **Missing**. Each item should
give a one-line reason and a recommended fix. Follow
[Code Reviews](reviews.md) for pull request scope and feedback rules.
