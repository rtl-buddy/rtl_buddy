## Validating Against The Project Template

For end-user behavior changes, use the template's local-development worktree. Point its editable `rtl_buddy` source at the exact feature worktree being tested.

```bash
# In a sibling clone of rtl-buddy-project-template:
git worktree add .worktrees/dev-local dev/local-rtl-buddy
cd .worktrees/dev-local
uv sync
uv run rb regression -c regression.yaml
```

Follow the template's `AGENTS.md`. Keep `dev/local-rtl-buddy` local and do not merge it to `main`.
