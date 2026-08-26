## Project-local env defaults: `.rtl-buddy/.env`

Store project-specific, untracked machine values in `.rtl-buddy/.env` beside `root_config.yaml`:

```sh
RTL_BUDDY_SLANG_PLUGIN=/opt/rtl-buddy-tools/yosys-slang/build/slang.so
SYSTEMC_HOME=/opt/homebrew/opt/systemc
RB_TOOLS=/Users/me/tools/rtl-buddy
```

Every command loads this file after discovering the project root and passes the values to tool subprocesses.

- Existing process environment variables win; the file provides fallback values only.
- Values are literal. There is no variable interpolation or escape processing; matching surrounding quotes are removed.
- Lines must use `KEY=VALUE`; comments and an optional `export ` prefix are accepted.
- Add `.rtl-buddy/.env` to `.gitignore`. `rb skill print-gitignore` prints the recommended entry.

Explicit YAML configuration still wins over environment fallback where a field supports both. A malformed env line fails with its file and line number.
