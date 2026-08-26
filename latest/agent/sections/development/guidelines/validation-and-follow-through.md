## Validation And Follow-Through

Let validation scale with risk:

- Docs-only edits: run frontmatter and MkDocs strict checks.
- CLI help changes: regenerate `docs/reference/cli.md` and run the generated-reference check.
- Path, artifact, or subprocess changes: add focused tests proving roots, generated paths, and subprocess `cwd`.
- Shared command-dispatch or config-loader changes: run the affected test module subset, then broaden if the change crosses command families.

Report skipped checks and the reason. Complete the applicable follow-through:

1. If CLI command names, flags, help text, or output behavior changed, regenerate `docs/reference/cli.md` with `uv run python scripts/gen_cli_reference.py`.
2. If a feature, command, optional extra, or external tool dependency changed, update `docs/install.md`.
3. If an external tool dependency changed, update `src/rtl_buddy/tool_manifest.py`, `tests/test_tool_manifest.py`, and `docs/concepts/tool-check.md` when tool-check behavior or coverage changes.
4. If docs changed, keep frontmatter valid and run the docs build. See [Documentation Guidelines](https://rtl-buddy.github.io/rtl_buddy/v6/development/docs/).
5. If behavior, YAML schema, version expectations, or validation workflows changed, update user docs and the bundled skill if agents rely on the behavior.
6. If release or packaging behavior changed, verify wheel inclusion rules and update downstream integrations after release.
7. Record new non-conventional behavior in `docs/known-issues.md`.
8. For `version/major`, add a complete `## vN to vM` section to `docs/migrations.md`.
