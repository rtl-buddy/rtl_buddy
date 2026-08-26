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
