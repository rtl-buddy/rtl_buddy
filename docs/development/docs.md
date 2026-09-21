---
description: Rules for concise, operational rtl_buddy documentation, including page types, content ownership, structure, review, and validation.
---

# Documentation Guidelines

These rules apply to `docs/`, the website and the local reference exposed by `rb docs list` and `rb docs show`.

## Write Operational Documentation

Help readers complete a task or make a decision with the current release.

- Lead with what the feature does, when to use it, or the command to run.
- Put prerequisites before the procedure and expected results after it.
- Keep commands and their required context together.
- Preserve constraints, defaults, failure behavior, and recovery steps.
- Use present tense and describe current behavior only.
- Remove origin stories, implementation chronology, release-by-release narration, and roadmap discussion. Put required upgrade history in `migrations.md`.
- Delete commentary that does not change what a reader should do.

Prefer a short, complete page. Keep information required to use the feature safely.

## State Each Fact Once

Give each rule, schema, or workflow one canonical home and link to it elsewhere.

- Do not repeat CLI option reference already generated in `reference/cli.md`.
- Do not copy YAML field definitions across concept pages; link to `reference/yaml.md`.
- Fold new behavior into the section that owns it. Do not append a release note or correction to otherwise stale prose.
- Merge overlapping sections. Delete superseded text instead of preserving both versions.
- Keep page boundaries task-based. A page should answer one recognizable user need.

## Structure for Retrieval

Agents may fetch one section with `rb docs show slug#anchor`, so every H2 must make sense on its own.

- Use descriptive headings based on tasks or decisions.
- Keep one topic per H2.
- Start each section with its answer, then add commands, constraints, or examples.
- Prefer short prose. Use lists for choices, procedures, and checklists; use tables only for comparison.
- Avoid H3 headings when separate H2 sections or tighter prose are clearer.
- Keep examples minimal and runnable. Show only the fields or output relevant to the point.

## Frontmatter

Every page except generated `reference/cli.md` starts with an accurate one- or two-sentence `description`:

```markdown
---
description: One or two sentences describing what this page covers.
---

# Page Title
```

The description is the page summary in `rb docs list` and `rb docs show --machine`; agents use it to decide what to fetch. State what the page covers, not that it "explains" something. CI checks it with `scripts/check_docs_frontmatter.py --check`.

Start content with one H1 and a sentence stating what the page helps the reader do. Use only necessary sections; do not add generic Overview, Background, or Conclusion sections.

## Page Types

Concept and guide pages under `concepts/`, plus `quickstart.md`, `install.md`,
`agents.md`, and `migrations.md`, teach a reader how and why to use a feature.
They must:

- Open with what the feature is and when or why to use it.
- Build from motivation to mechanics.
- Use scannable section headings.
- Include runnable examples for non-trivial patterns.
- Stay focused on one concept and avoid unexplained field enumeration.

Reference pages are intentionally exhaustive. Evaluate `reference/cli.md` and
`reference/yaml.md` for accuracy and completeness rather than narrative flow.
Field tables, flag descriptions, and schema examples are appropriate there.

## Generated Pages

`docs/reference/cli.md` is generated from CLI help by `scripts/gen_cli_reference.py`. Edit help strings in `src/rtl_buddy/rtl_buddy.py`, then regenerate; do not edit the page by hand. The generator owns its frontmatter, and CI auto-commits drift.

## Review Documentation

For a repository-wide review:

1. List the shipped pages with `uv run rb docs list`.
2. Classify each page as a concept/guide or reference page.
3. Check each page against the authoring rules above.
4. Compare `reference/cli.md` with `uv run rb --help` and relevant command help.
5. Compare `reference/yaml.md` with `src/rtl_buddy/config/` for missing fields.

For a targeted feature review, identify the user-visible behavior in the
change, check that a concept page explains it, verify new YAML fields in
`reference/yaml.md`, and regenerate `reference/cli.md` when CLI help changes.

For a full review, report `Page | Type | Criterion | Status | Note`. For a
targeted review, list each gap with one recommended action. Treat missing
`description:` frontmatter as CI-blocking. Follow [Code Reviews](reviews.md) for
pull request scope and feedback rules.

## Local Checks

Run the docs checks before opening a PR that touches docs:

```bash
uv run python scripts/check_docs_frontmatter.py --check
npm run build
```

For CLI help changes, regenerate the CLI reference first:

```bash
uv run python scripts/gen_cli_reference.py
```
