## Bundled agent skills

The wheel includes a version-matched skill family for Claude Code and Codex. The primary `rtl-buddy` skill routes advanced work to focused test, dispatch, graph, formal, and implementation skills.

```bash
rb skill install
rb skill status
rb skill uninstall
```

Install scope determines the target:

| Scope | Claude Code | Codex |
| --- | --- | --- |
| User (default) | `~/.claude/skills/<member>/SKILL.md` | `~/.codex/skills/<member>/SKILL.md` |
| Project (`--project`) | `<root>/.claude/skills/<member>/SKILL.md` | `<root>/.agents/skills/<member>/SKILL.md` |
| Explicit dir (`--dir PATH`) | `<PATH>/<member>/SKILL.md` | — |

`<member>` is `rtl-buddy`, `rtl-buddy-test`, `rtl-buddy-dispatch`, `rtl-buddy-graph`, `rtl-buddy-fpv`, or `rtl-buddy-implementation`.

Use project scope only to override user-level skills for a project pinned to a different major. Project discovery walks up for `root_config.yaml`, then `.git/`. Use `--dir PATH` for a flat family outside the normal layout; it cannot be combined with `--project` or `--root`.

Re-run installation after upgrading. It refreshes every member and removes obsolete skill directories at the selected scope. Install or uninstall once at every scope you use. Project installation updates `.gitignore`; pass `--no-gitignore` to suppress that edit.
