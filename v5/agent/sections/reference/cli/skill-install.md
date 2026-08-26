## skill install

```text
Usage: rtl-buddy skill install [OPTIONS]                                               
                                                                                        
 Install the bundled rtl_buddy skill.                                                   
                                                                                        
 Default scope is user-level (`~/.claude/skills/rtl_buddy/` and                         
 `~/.codex/skills/rtl_buddy/`). Use `--project` to install into the                     
 discovered project root instead; project-level copies take precedence                  
 over user-level when both exist.                                                       
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --project                install into the discovered project root instead of the     │
│                          user home                                                   │
│ --root             PATH  explicit target root (implies project-level layout)         │
│ --no-claude              skip writing the Claude Code target                         │
│ --no-codex               skip writing the Codex target                               │
│ --dry-run                print what would be written and exit                        │
│ --force                  overwrite even when content matches                         │
│ --help                   Show this message and exit.                                 │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
