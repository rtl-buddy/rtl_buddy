---
name: audit-bundled-skill
description: Audit rtl_buddy's bundled skills for lean, accurate guidance and correct routing. Use for content or installer reviews.
---

# audit-bundled-skill

Review the primary `src/rtl_buddy/skill/SKILL.md` and specialist
`src/rtl_buddy/skill/<name>/SKILL.md` files shipped in the rtl_buddy wheel.

## Design principles

The family must stay lean. Its purpose is agent workflow guidance, not
documentation. The primary covers basic use and routing; a specialist contains
only non-obvious guidance for its topic. Anything deeper belongs in bundled docs
and should be cited instead.

**Every bundled skill should:**
- Stay under 8 KiB
- Match its directory name in frontmatter and have a unique, discriminating description
- Include the version check instruction (`rb --version` at the top of every run summary)
- Route option lists, schemas, and how-tos to local `rb docs show <page>` references

**The primary should:**
- Explain what each major feature does, when to use it, and a basic valid command
- Cover `--machine`, result interpretation, YAML orientation, CWD/output
  anchoring, and specialist routing
- Give an agent enough to start ordinary work correctly without loading a specialist

**Each specialist should:**
- Be self-contained when selected without the primary
- Contain only non-obvious decision guidance and gotchas for its topic
- Avoid repeating the primary's general YAML, CWD, and result orientation

**The family must not:**
- Restate YAML schemas, field references, examples, option lists, or flag
  descriptions — those live in `docs/reference/`
- Add a specialist merely because rtl_buddy gained a command — add one only
  when distinct agent behavior would otherwise be wrong
- Duplicate docs-site content beyond brief operational orientation

## How to audit

1. Enumerate every bundled skill from `SKILL_DIRNAMES`; check <8 KiB and
   directory/frontmatter name equality.
2. Check that descriptions are unique and discriminating enough for automatic selection.
3. Read each section. Ask whether an agent needs it to act correctly before opening docs.
4. Move reference detail, option lists, schemas, and worked how-tos to docs citations.
5. Check current CLI help against commands the family describes; flag stale guidance.
6. Check install/status/uninstall tests when family membership changes.

## Output format

List findings under two headings: **Trim** (content that should be removed or
replaced with a docs cite) and **Missing** (agent-specific behavior the skill
omits that would cause incorrect agent behavior). For each item, give a one-line
explanation and the recommended fix.
