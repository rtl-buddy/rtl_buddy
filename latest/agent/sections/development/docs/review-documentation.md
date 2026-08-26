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
`description:` frontmatter as CI-blocking. Follow [Code Reviews](https://rtl-buddy.github.io/rtl_buddy/v6/development/reviews/) for
pull request scope and feedback rules.
