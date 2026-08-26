## skill install

```text
Usage: rtl-buddy skill install [OPTIONS]

 Install the bundled rtl_buddy skill family.

 Default scope is user-level (`~/.claude/skills/rtl-buddy/` and
 `~/.codex/skills/rtl-buddy/`). Use `--project` to install into the
 discovered project root instead; project-level copies take precedence
 over user-level when both exist. Use `--dir PATH` to write the family as
 sibling directories under PATH, bypassing the `.claude`/`.agents` layout.

 A marked sibling `rtl_buddy/` directory is removed to prevent a stale
 duplicate of the primary skill.

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --project                   install into the discovered project root instead of the  │
│                             user home                                                │
│ --root                PATH  explicit target root (implies project-level layout)      │
│ --dir                 PATH  write the skill family directly under <DIR>/, bypassing  │
│                             the .claude/.agents/.codex layout                        │
│ --no-claude                 skip writing the Claude Code target                      │
│ --no-codex                  skip writing the Codex target                            │
│ --no-gitignore              skip updating .gitignore on project-level installs       │
│ --dry-run                   print what would be written and exit                     │
│ --force                     overwrite even when content matches                      │
│ --help                      Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
