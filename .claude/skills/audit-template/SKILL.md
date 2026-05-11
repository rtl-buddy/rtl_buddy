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
2. Classify the intended role of the example:
   - **sandbox flow**: part of the integrated reference design. It should connect to the same DUT/spec/model/test/regression story and demonstrate a continuous workflow, not a disconnected side demo.
   - **template demo**: a minimal isolated feature example. It may be disjoint from sandbox, but should stay small, readable, and focused on one rtl_buddy capability.
3. If it appears, assess:
   - Is it **isolated** — can a reader find and read it without wading through unrelated content?
   - Is it **integrated/minimal** — for sandbox, does it participate in the continuous workflow; for template, is it a small feature demo?
   - Is it **explained** — does a comment, README section, or inline annotation say what it does and when to use it?
   - Is it **runnable** — is there a concrete `uv run rb ...` command a reader can copy and run?
4. If it does not appear, flag it as a gap.

Flag any feature placed in sandbox that behaves like a disconnected template demo, and any template example that is too large or entangled to serve as a minimal reference.

## Step 3 — Report

Output a table:

| Feature / Change | rtl_buddy commit | Intended role | Template location | Isolated | Integrated/Minimal | Explained | Runnable | Action needed |
|------------------|------------------|---------------|-------------------|----------|--------------------|-----------|----------|---------------|

Mark each cell ✅ / ❌ / ⚠️ (partial). After the table, emit a **Gap Summary** with one recommended action per ❌ or ⚠️ row.

## Quality bar

The standard is: a competent RTL engineer with no prior rtl_buddy experience can read the template and immediately understand how to use each feature. Don't assess the template charitably — if an example exists but is unexplained or buried, that counts as a gap.
