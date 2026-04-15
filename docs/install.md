# Installation

## Current distribution: git submodule

`rtl_buddy` is currently distributed as a git submodule inside your RTL project.

### Setup

1. Add `rtl_buddy` as a submodule under `tools/rtl_buddy`:

   ```bash
   git submodule add <rtl_buddy-repo-url> tools/rtl_buddy
   ```

2. Point `requirements.txt` at the submodule:

   ```text
   -e tools/rtl_buddy
   ```

3. Install from `requirements.txt` in your project's setup script:

   ```bash
   pip install -r requirements.txt
   ```

4. Verify:

   ```bash
   rtl-buddy --version
   ```

### Updating

To update to a newer commit:

```bash
git -C tools/rtl_buddy fetch
git -C tools/rtl_buddy checkout <tag-or-sha>
git add tools/rtl_buddy
git commit -m "Update rtl_buddy to <version>"
```

## Planned distribution: uv / git install

A `uv`-based install path is planned as the primary distribution mechanism. This page will be updated when that path is available.

The intended form will be:

```bash
uv add "rtl_buddy @ git+<repo-url>@<tag>"
```

See [Submodule to uv migration](migrations/submodule-to-uv.md) for transition guidance when this path lands.

## Requirements

- Python 3.11 or later
- Simulation tool: Verilator (macOS/Linux) or VCS (Linux)
- Optional: Verible for lint and syntax checks
