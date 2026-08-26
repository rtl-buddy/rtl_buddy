## Subcommand: `--required-for`

```bash
rb tool-check --required-for fpv
```

Narrows the report to just the tools whose `used_by:` includes the named subcommand. Pairs naturally with `--strict` for a "is `rb fpv` ready right now?" CI check:

```bash
rb tool-check --required-for fpv --strict || \
  { echo "rb fpv is not ready — see above"; exit 1; }
```

Exit code semantics under `--required-for` differ slightly from the default — see [Exit codes](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/tool-check/#exit-codes) below.
