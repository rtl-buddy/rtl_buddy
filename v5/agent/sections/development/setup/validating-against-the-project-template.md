## Validating Against The Project Template

For changes that affect end-user behavior, validate against the [rtl-buddy-project-template](https://github.com/rtl-buddy/rtl-buddy-project-template) before opening a PR. The template's `dev/local-rtl-buddy` branch swaps the PyPI pin for an editable path dependency on a sibling `rtl_buddy/` checkout, so you can iterate locally without publishing.

Typical loop:

```bash
# In a sibling clone of rtl-buddy-project-template:
git worktree add .worktrees/dev-local dev/local-rtl-buddy
cd .worktrees/dev-local
uv sync                              # picks up ../../../rtl_buddy editable
uv run rb regression -c regression.yaml
```

The template's `AGENTS.md` documents the same standing-branch convention; do not push that branch back to `main`.
