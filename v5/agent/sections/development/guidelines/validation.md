## Validation

Let validation scale with risk:

- Docs-only edits: run frontmatter and MkDocs strict checks.
- CLI help changes: regenerate `docs/reference/cli.md` and run the generated-reference check.
- Path, artifact, or subprocess changes: add focused tests proving roots, generated paths, and subprocess `cwd`.
- Shared command-dispatch or config-loader changes: run the affected test module subset, then broaden if the change crosses command families.

Report skipped checks in the PR with the reason.
