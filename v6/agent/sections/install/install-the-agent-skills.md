## Install the agent skills

Install the bundled Claude Code and Codex skill family at user scope:

```bash
uv run rb skill install
```

For a project pinned to a different RTL Buddy major, install a project-local override:

```bash
uv run rb skill install --project
```

Re-run installation after upgrading to refresh every family member. See [Agent Use](https://rtl-buddy.github.io/rtl_buddy/v6/agents/#bundled-agent-skills) for members, paths, status checks, and project `.gitignore` guidance.
