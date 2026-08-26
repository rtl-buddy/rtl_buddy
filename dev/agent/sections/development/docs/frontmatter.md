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
