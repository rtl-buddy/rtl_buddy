---
name: audit-template
description: Audit the rtl-buddy-project-template against recent rtl_buddy changes. Use when asked to check whether the template is up to date, review what's missing, or sync the template to new features.
---

# audit-template

You are auditing `rtl-buddy-project-template` (sibling to `rtl_buddy/`) against recent `rtl_buddy` changes.

The goal is to find features or behaviors that exist in `rtl_buddy` but are absent or poorly explained in the template. You are not ticking a static checklist — you are doing a delta: *what changed, and is the template keeping up?*

## Step 1 — Discover recent rtl_buddy additions

Run `git log --oneline` in `rtl_buddy/` to get the recent commit history. Choose a meaningful window (e.g. since the last template pin bump in `rtl-buddy-project-template/pyproject.toml`, or the last N commits if you have a specific task).

For each interesting commit, look at the diff to determine whether it added or changed:
- A CLI command, flag, or option (check `src/rtl_buddy/rtl_buddy.py`, `docs/reference/cli.md`)
- A YAML field or config section (check `docs/reference/yaml.md`, `src/rtl_buddy/config/`)
- A concept or workflow (check `docs/concepts/`)
- A plugin hook behavior (check `docs/concepts/plugins.md`)
- Pass/fail detection behavior (check `src/rtl_buddy/tools/`)

Focus on user-visible changes. Skip internal refactors that don't affect behavior or config.

## Step 2 — Check the template

For each user-visible change identified:

1. Search `rtl-buddy-project-template/` for whether the feature appears in any config file, SV file, plugin script, or README.
2. If it appears, assess:
   - Is it **isolated** — can a reader find and read it without wading through unrelated content?
   - Is it **explained** — does a comment, README section, or inline annotation say what it does and when to use it?
   - Is it **runnable** — is there a concrete `uv run rb ...` command a reader can copy and run?
3. If it does not appear, flag it as a gap.

## Step 3 — Report

Output a table:

| Feature / Change | rtl_buddy commit | Template location | Isolated | Explained | Runnable | Action needed |
|------------------|------------------|-------------------|----------|-----------|----------|---------------|

Mark each cell ✅ / ❌ / ⚠️ (partial). After the table, emit a **Gap Summary** with one recommended action per ❌ or ⚠️ row.

## Quality bar

The standard is: a competent RTL engineer with no prior rtl_buddy experience can read the template and immediately understand how to use each feature. Don't assess the template charitably — if an example exists but is unexplained or buried, that counts as a gap.
