---
description: Authoring and review guidelines for rtl_buddy documentation, including page types, structure, generated files, completeness, and validation.
---

# Documentation Guidelines

These rules apply to files under `docs/`.
The docs are both the human-facing site and the local reference surface exposed by `rb docs list` and `rb docs show`.

## Frontmatter

Every docs page must start with a YAML frontmatter block containing a `description:` field:

```markdown
---
description: One or two sentences describing what this page covers.
---

# Page Title
```

The `description:` value is used as the page summary in `rb docs list` and `rb docs show --machine`.
Agents read it to decide which page to fetch; make it accurate and specific.

Rules:

- Required on every page except `reference/cli.md`, which is auto-generated.
- One or two sentences; focus on what the page covers, not that it "explains" or "describes" something.
- CI enforces this via `scripts/check_docs_frontmatter.py --check`.

## Writing Style

Write for both humans and agents:

- Be concise. Agents parse these pages programmatically. Long preambles add noise.
- Be complete. Every H2 section should stand alone. Agents may fetch a single section via `rb docs show slug#anchor`.
- Keep one topic per H2. If a section covers two things, split it.
- Prefer prose for explanations. Bullets are fine for option lists, checklists, and step sequences.

## Page Structure

Use this shape for hand-written pages:

```markdown
---
description: ...
---

# Title

Opening sentence or short paragraph that orients the reader.

## Section One

Content.

## Section Two

Content.
```

Avoid deeply nested subsections (`###` and below) when the content can be reorganized into top-level H2 sections.

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

`docs/reference/cli.md` is generated from `rtl-buddy --help` output by `scripts/gen_cli_reference.py`.
Do not edit it by hand; changes will be overwritten.
Edit CLI help strings in `src/rtl_buddy/rtl_buddy.py` instead.

CI auto-commits regenerated `cli.md` if it drifts.
Its `description:` frontmatter is part of the generated output and is maintained by the generator, not by hand.

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
uv run --group docs mkdocs build --strict
```

For CLI help changes, regenerate the CLI reference first:

```bash
uv run python scripts/gen_cli_reference.py
```
