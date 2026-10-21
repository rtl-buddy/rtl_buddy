## Clone And Sync

```bash
git clone https://github.com/rtl-buddy/rtl_buddy.git
cd rtl_buddy
uv sync --group dev
```

This installs the package and the lint, test, and docs groups. Add `--extra graph-extract` only when working on the optional graph binding tier.

Verify the install:

```bash
uv run rb --version
```
