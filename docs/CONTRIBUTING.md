---
description: Guidelines for contributing to rtl_buddy documentation, including the frontmatter convention, writing style, and what is auto-generated.
---

# Contributing to Docs

This page covers the standards for writing and maintaining `rtl_buddy` documentation.
It applies to everyone editing files under `docs/`. It is also accessible via `rb docs show CONTRIBUTING`.

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
- Required on every page except `reference/cli.md` (auto-generated — see below)
- One or two sentences; focus on what the page covers, not that it "explains" or "describes" something
- CI enforces this via `scripts/check_docs_frontmatter.py --check`

## Writing style

Write for both humans and agents:

- **Be concise.** Agents parse these pages programmatically. Long preambles add noise.
- **Be complete.** Every H2 section should stand alone. Agents may fetch a single section via `rb docs show slug#anchor`.
- **One topic per H2.** If a section covers two things, split it.
- **Prose over bullets** for explanations. Bullets are fine for option lists and step sequences.

## Page structure

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

## Auto-generated pages

`docs/reference/cli.md` is generated from `rtl-buddy --help` output by `scripts/gen_cli_reference.py`.
Do not edit it by hand — changes will be overwritten. Edit CLI help strings in `src/rtl_buddy/rtl_buddy.py` instead.

CI auto-commits regenerated `cli.md` if it drifts. Its `description:` frontmatter is part of the generated output and is maintained by the generator, not by hand.
