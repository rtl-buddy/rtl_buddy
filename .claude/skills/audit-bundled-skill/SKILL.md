---
name: audit-bundled-skill
description: Audit the rtl_buddy bundled SKILL.md for adherence to its design principles. Use when asked to review, update, or check the skill file at src/rtl_buddy/skill/SKILL.md.
---

# audit-bundled-skill

You are reviewing `src/rtl_buddy/skill/SKILL.md` — the agent skill that ships inside the rtl_buddy wheel.

## Design principles

The bundled skill must stay lean. Its purpose is agent workflow guidance, not documentation. It may include brief operational orientation when that helps agents find files, run commands, interpret results, or inspect outputs without reading docs first. Anything deeper belongs in the docs site and should be cited instead.

**The skill should:**
- Stay at or under 60 lines
- Cover agent-specific conventions that are not obvious from the docs: `--machine` flag requirement, JSONL log format, CWD rules for `test` vs `regression`, artefact paths
- Include the local docs commands so agents know how to reach bundled references
- Include a brief YAML type overview so agents can find config files and understand their role quickly
- Include concise pass/fail detection guidance for UVM, cocotb, and default stdout parsing
- Include concise artefact/log locations needed for debugging and summaries
- Reference docs via `rtl-buddy docs show <page>` rather than restating content inline
- Give the agent enough to run correctly without reading the docs first
- Include the version check instruction (`rtl-buddy --version` at top of every run)

**The skill must not:**
- Restate YAML schemas, field references, examples, option lists, or flag descriptions — those live in `docs/reference/`
- Grow feature-by-feature as rtl_buddy adds commands — only add lines when agent behavior would otherwise be wrong
- Duplicate docs-site content beyond brief operational orientation

## How to audit

1. Count lines. If over 60, identify what can be moved to a docs cite.
2. Read each section. For every paragraph, ask: does an agent need this to act correctly or debug quickly without opening docs first?
3. Distinguish concise orientation from reference duplication. Keep short file-purpose, pass/fail, and artefact guidance; trim field tables, examples, option details, and command-specific feature docs.
4. Check the current CLI (`rtl-buddy --help` and per-command `--help`) against what the skill describes. Flag anything stale.
5. Check that the skill cites the docs site for all feature-specific content rather than embedding it.

## Output format

List findings under two headings: **Trim** (content that should be removed or replaced with a docs cite) and **Missing** (agent-specific behavior the skill omits that would cause incorrect agent behavior). For each item, give a one-line explanation and the recommended fix.
