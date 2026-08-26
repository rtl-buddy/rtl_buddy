## Required Follow-Through

After meaningful `rtl_buddy` changes:

1. If CLI command names, flags, help text, or output behavior changed, regenerate `docs/reference/cli.md` with `uv run python scripts/gen_cli_reference.py`.
2. If a feature, command, optional extra, or external tool dependency changed, update `docs/install.md`.
3. If an external tool dependency changed, update `src/rtl_buddy/tool_manifest.py`, `tests/test_tool_manifest.py`, and `docs/concepts/tool-check.md` when tool-check behavior or coverage changes.
4. If docs changed, keep frontmatter valid and run the docs build. See [Documentation Guidelines](https://rtl-buddy.github.io/rtl_buddy/v5/development/docs/).
5. If behavior, YAML schema, version expectations, or validation workflows changed, update user docs and the bundled skill if agents rely on the behavior.
6. If release or packaging behavior changed, verify wheel inclusion rules and update downstream integrations after release.
7. If you discovered or introduced a quirk or non-conventional behavior, add an entry to `docs/known-issues.md`. Treat this as a default step, not an afterthought.
8. If the change is a `version/major` bump, add or update the `docs/migrations/vN-to-vM.md` page (and its `mkdocs.yml` nav entry) covering every breaking behavior change. See [Releases](https://rtl-buddy.github.io/rtl_buddy/v5/development/guidelines/#releases).
