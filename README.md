# `rtl_buddy`

CLI tool for automating tests, regressions, and RTL workflows for Verilog/SystemVerilog codebases.

## Documentation

Full documentation lives in [`docs/`](docs/) and is built with MkDocs.

To serve docs locally:

```bash
uv sync --group docs
uv run mkdocs serve
```

## Quick Start

Run a test:

```bash
rtl-buddy test basic
```

Run a regression:

```bash
rtl-buddy regression
```

For full usage, see [docs/quickstart.md](docs/quickstart.md).

## Installation

See [docs/install.md](docs/install.md).

## Known Issues

See [docs/known-issues.md](docs/known-issues.md).
