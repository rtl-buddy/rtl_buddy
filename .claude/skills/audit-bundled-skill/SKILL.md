---
name: audit-bundled-skill
description: Audit the rtl_buddy bundled SKILL.md for adherence to its design principles. Use when asked to review, update, or check the skill file at src/rtl_buddy/skill/SKILL.md.
---

# audit-bundled-skill

You are reviewing `src/rtl_buddy/skill/SKILL.md` — the agent skill that ships inside the rtl_buddy wheel.

## Design principles

The bundled skill must stay lean. Its purpose is agent workflow guidance, not documentation. Anything that belongs in the docs site should cite the docs site instead.

**The skill should:**
- Stay at or under 60 lines
- Cover agent-specific conventions that are not obvious from the docs: `--machine` flag requirement, JSONL log format, CWD rules for `test` vs `regression`, artefact paths
- Reference docs via `rtl-buddy docs show <page>` rather than restating content inline
- Give the agent enough to run correctly without reading the docs first
- Include the version check instruction (`rtl-buddy --version` at top of every run)

**The skill must not:**
- Restate YAML schemas, field references, or flag descriptions — those live in `docs/reference/`
- Grow feature-by-feature as rtl_buddy adds commands — only add lines when agent behavior would otherwise be wrong
- Duplicate content between the skill and the docs site

## How to audit

1. Count lines. If over 60, identify what can be moved to a docs cite.
2. Read each section. For every paragraph, ask: does an agent need this to run correctly, or would it be equally served by `rtl-buddy docs show <page>`?
3. Check the current CLI (`rtl-buddy --help` and per-command `--help`) against what the skill describes. Flag anything stale.
4. Check that the skill cites the docs site for all feature-specific content rather than embedding it.

## Output format

List findings under two headings: **Trim** (content that should be removed or replaced with a docs cite) and **Missing** (agent-specific behavior the skill omits that would cause incorrect agent behavior). For each item, give a one-line explanation and the recommended fix.
