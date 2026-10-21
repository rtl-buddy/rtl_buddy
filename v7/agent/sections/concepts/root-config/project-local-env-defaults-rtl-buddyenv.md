## Project-local env defaults: `.rtl-buddy/.env`

Put untracked, machine-specific values in `.rtl-buddy/.env` beside `root_config.yaml`:

```sh
RTL_BUDDY_SLANG_PLUGIN=/opt/rtl-buddy-tools/yosys-slang/build/slang.so
SYSTEMC_HOME=/opt/homebrew/opt/systemc
RB_TOOLS=/Users/me/tools/rtl-buddy
```

Every command loads this file after finding the project root and passes the values to tool subprocesses.

- Variables already in the process environment win; the file only supplies fallbacks.
- Explicit YAML configuration wins over the environment fallback where a field supports both.
- Lines are `KEY=VALUE`, with `#` comments and an optional `export ` prefix. Values are literal: no interpolation or escapes, and matching surrounding quotes are removed.
- A malformed line fails with its file and line number.
- Add `.rtl-buddy/.env` to `.gitignore`. `rb skill print-gitignore` prints the recommended entry.
