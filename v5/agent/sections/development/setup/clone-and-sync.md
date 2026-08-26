## Clone And Sync

```bash
git clone https://github.com/rtl-buddy/rtl_buddy.git
cd rtl_buddy
uv sync --group dev
```

`uv sync --group dev` installs the package plus the composite `dev` dependency group (lint, test, docs). The resulting environment lives in `.venv/`; `uv run <cmd>` and `./venv/bin/python -m rtl_buddy …` both reach it.

Verify the install:

```bash
uv run rb --version
```
