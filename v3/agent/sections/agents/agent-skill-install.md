## Agent Skill Install

The `rtl_buddy` wheel bundles an agent skill for Claude Code and Codex. Users run a one-time install to materialize it into their agent skill directories:

```bash
rtl-buddy skill install             # default: user-level
rtl-buddy skill install --project   # project-level (overrides user-level for that project)
rtl-buddy skill install --root PATH # explicit target (implies project-level layout)
rtl-buddy skill status              # report installed version vs current
rtl-buddy skill uninstall           # remove skill files
```

Targets:

| Scope | Claude Code | Codex |
|-------|-------------|-------|
| User (default) | `~/.claude/skills/rtl_buddy/SKILL.md` | `~/.codex/skills/rtl_buddy/SKILL.md` |
| Project (`--project`) | `<root>/.claude/skills/rtl_buddy/SKILL.md` | `<root>/.agents/skills/rtl_buddy/SKILL.md` |

User-level is the recommended default — one install per machine, one place to keep current. Project-level is an opt-in override for projects pinned to a divergent `rtl_buddy` major; Claude Code's resolution order puts the project copy first, so both can coexist.

For project-level installs, add the target dirs to your project's `.gitignore` — the install command prints the exact lines. `rtl-buddy skill install --project` discovers the project root via `root_config.yaml` (falling back to `.git/`), so it is safe to run from a `verif/` subdirectory.
