## Set Up The Agent Skill

`rtl_buddy` ships an agent skill for Claude Code and Codex. After installing `rtl_buddy`, run once per machine:

```bash
uv run rb skill install
```

This writes `SKILL.md` to `~/.claude/skills/rtl_buddy/` and `~/.codex/skills/rtl_buddy/`. Agents pick it up automatically. Re-run after upgrading `rtl_buddy` to refresh the content.

To install at project scope instead (overrides the user-level copy for that project):

```bash
uv run rb skill install --project
```

See [For Agents](https://rtl-buddy.github.io/rtl_buddy/v3/agents/) for scope semantics and `.gitignore` guidance.
