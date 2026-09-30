## Bundled agent skills

The wheel ships a version-matched skill family for Claude Code and Codex. The primary `rtl-buddy` skill routes advanced work to focused test, dispatch, graph, formal, and implementation skills.

```bash
rb skill install
rb skill status
rb skill uninstall
```

| Scope | Claude Code | Codex |
| --- | --- | --- |
| User (default) | `~/.claude/skills/<member>/SKILL.md` | `~/.codex/skills/<member>/SKILL.md` |
| Project (`--project`) | `<root>/.claude/skills/<member>/SKILL.md` | `<root>/.agents/skills/<member>/SKILL.md` |
| Explicit dir (`--dir PATH`) | `<PATH>/<member>/SKILL.md` | — |

`<member>` is `rtl-buddy`, `rtl-buddy-test`, `rtl-buddy-dispatch`, `rtl-buddy-graph`, `rtl-buddy-fpv`, or `rtl-buddy-implementation`.

- Use project scope only to override user-level skills in a project pinned to a different major version. The project root is found by walking up for `root_config.yaml`, then `.git/`.
- Use `--dir PATH` for a flat family outside the normal layout. It cannot be combined with `--project` or `--root`.
- Installing refreshes every member and removes obsolete skill directories at that scope. Install or uninstall once per scope you use. Re-run it after upgrading.
- Project installation updates `.gitignore`. Pass `--no-gitignore` to skip that.
