## Set Content Boundaries

Every skill must:

- Stay under 8 KiB.
- Match its directory name in frontmatter.
- Have a description that distinguishes it from every other family member.
- Require `rb --version` at the top of each run summary.
- Link option lists, schemas, and procedures through `rb docs show <page>` instead of copying them.

The primary skill covers the minimum needed for ordinary work: feature selection, basic commands, `--machine`, result interpretation, YAML orientation, path anchoring, and specialist routing.

A specialist must work when selected alone and contain only non-obvious decisions and failure modes for its topic. Do not repeat general guidance from the primary or create a specialist solely because a command exists.
