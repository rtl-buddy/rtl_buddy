## verible lint

```text
Usage: rtl-buddy verible lint [OPTIONS] [VERIBLE_ARGS]...

 run verible-verilog-lint

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│   verible_args      [VERIBLE_ARGS]...                                                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --model          TEXT  Model name from models.yaml whose filelist supplies the files │
│                        to visit (repeatable). Bare source entries only: -v/-y        │
│                        library files and +incdir+/+define+/+libext+ directives are   │
│                        dropped, then the cfg-verible `exclude` globs and --exclude   │
│                        filter the rest.                                              │
│ --exclude        TEXT  Glob of project-root-relative paths dropped from --model      │
│                        expansion (repeatable, fnmatch semantics: * also crosses      │
│                        directory separators). Adds to the cfg-verible `exclude`      │
│                        list.                                                         │
│ --help                 Show this message and exit.                                   │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
