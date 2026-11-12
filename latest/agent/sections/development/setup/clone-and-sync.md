## Clone And Sync

```bash
git clone https://github.com/rtl-buddy/rtl_buddy.git
cd rtl_buddy
uv sync --group dev
```

This installs the package and the lint and test groups. Add `--extra graph-extract` when working on the optional graph binding tier or matching the CI test environment.

Verify the install:

```bash
uv run rb --version
```
