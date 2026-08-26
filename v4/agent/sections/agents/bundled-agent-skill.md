## Bundled agent skill

The `rtl_buddy` wheel ships an agent skill that teaches Claude Code and Codex the conventions for invoking `rtl_buddy` — when to use `--machine`, where logs are written, how multi-suite runs lay out artefacts, and which docs to consult. Because the skill ships with the wheel, its content is locked to the installed `rtl_buddy` major version.

Users materialize the skill once with `rtl-buddy skill install`:

```bash
rtl-buddy skill install             # default: user-level
rtl-buddy skill install --project   # project-level (overrides user-level for that project)
rtl-buddy skill uninstall           # remove skill files
```

See [cli reference](https://rtl-buddy.github.io/rtl_buddy/v4/reference/cli/) for full `rb skill` interface.

Install targets:

| Scope | Claude Code | Codex |
|-------|-------------|-------|
| User (default) | `~/.claude/skills/rtl_buddy/SKILL.md` | `~/.codex/skills/rtl_buddy/SKILL.md` |
| Project (`--project`) | `<root>/.claude/skills/rtl_buddy/SKILL.md` | `<root>/.agents/skills/rtl_buddy/SKILL.md` |

User-level is the default because the skill is workflow-pattern guidance that changes rarely across `rtl_buddy` versions; a single copy per machine encourages keeping `rtl_buddy` aligned across projects. Project-level installs are an opt-in override for projects pinned to a divergent `rtl_buddy` major — Claude Code's resolution order puts the project copy first, so both scopes can coexist.

For project-level installs, the install command prints the `.gitignore` lines to add. Project root is discovered by walking up for `root_config.yaml` (falling back to `.git/`), so `rtl-buddy skill install --project` is safe to run from a `verif/` subdirectory.
