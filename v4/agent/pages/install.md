---
description: How to install rtl_buddy into a project using uv, including prerequisites and verification steps.
---

# Installation

`rtl_buddy` is available on PyPI and installed into your project environment with `uv`.

## Prerequisites

- Python 3.11 or later
- `uv`

Everything else is feature-dependent: which external tools you need is decided by which `rb` commands you use. The matrix below maps each command to its required and optional tools.

## Dependency types

rtl_buddy classifies dependencies into four buckets:

- **Required dependency**: Installed automatically with the `rtl_buddy` wheel; no external setup.
- **Integrated tool**: A rtl_buddy feature is built around one specific tool; you must install that exact tool to use the feature with no alternatives supported.
- **Pluggable**: rtl_buddy defines an interface; any tool that fits the interface works. rtl_buddy does not know what the tool specifically is or does — it just hands it the inputs the interface promises and consumes the outputs the interface promises.
- **Pluggable, curated**: tools that plug into the same plug point as **Pluggable**, but rtl_buddy carries first-class optimizations triggered by the tool name (e.g. coverage merging tuned for a specific simulator, a two-stage flow when a specific synthesis backend is selected). Having curated tools does not prevent non-curated tools from plugging into the same plug points.

## Required dependencies

These are installed automatically when you `uv add rtl_buddy` — no action needed:

- `typer`, `click`, `pyserde[yaml]`, `ruamel.yaml`, `rich` — core CLI and config parsing.
- `pywellen` — FST/VCD waveform reader. Used by `rb wave` annotation regardless of which waveform viewer is configured; the data layer is viewer-independent, which is why it ships with the wheel rather than as a Surfer-side install step.

## External tools by feature

| Command / feature | Integration type | Curated tools | Sub-deps and notes |
|---|---|---|---|
| `rb test`, `rb randtest`, `rb regression` | Pluggable | Verilator, VCS (Icarus on the roadmap) | Install the `lcov` package in your OS for LCOV / HTML coverage export from Verilator runs. |
| `rb verible` | Integrated tool | Verible | `brew tap chipsalliance/verible && brew install verible` on macOS; or see [Verible releases](https://github.com/chipsalliance/verible/releases). |
| Coverview packaging (under `rb regression`) | Integrated tool | Antmicro [Coverview](https://github.com/antmicro/coverview) | Install the `info-process` package in your OS via Coverview's own setup for full package generation. |
| `rb synth`, `rb synth-regression` | Pluggable | `yosys`, `openroad` | `yosys` is required (the [rtl-buddy/yosys fork](https://github.com/rtl-buddy/yosys), see below); `openroad` is required only when `tool: openroad`. See [Synthesis](concepts/synthesis.md). |
| `rb pnr` | Integrated tool | OpenROAD ≥ `25Q1` | Optional: `klayout` for `--gds` / `--png` streamout and rendering. See [Place-and-Route](concepts/pnr.md). |
| `rb cdc`, `rb cdc-regression` | Integrated tool | [rtl-buddy-cdc](https://github.com/rtl-buddy/rtl-buddy-cdc) | SpyGlass support is on the roadmap — tracked in [issue #85](https://github.com/rtl-buddy/rtl_buddy/issues/85). |
| `rb wave` | Integrated tool | Surfer (rtl-buddy fork, `rtl-buddy` branch) | nvim for full annotation round-trip; any editor configurable via `editor-cmd` for one-way "open at line". Vaporview / VS Code support is on the roadmap — tracked in [issue #84](https://github.com/rtl-buddy/rtl_buddy/issues/84). See [Waveform Viewer](concepts/wave.md). |

### Forks required

rtl_buddy currently validates against two forks rather than upstream:

- **Surfer** — required. Use the [`rtl-buddy/surfer`](https://github.com/rtl-buddy/surfer) repo, branch `rtl-buddy`. Mainline Surfer works for basic FST viewing but does not support the WCP signal-value annotation features `rb wave` relies on.
- **Yosys** — required. Use the [`rtl-buddy/yosys`](https://github.com/rtl-buddy/yosys) repo, which tracks upstream with rtl-buddy-specific patches.

Build instructions live on the respective concept pages: [Surfer build](concepts/wave.md#surfer-build) and [Installing Yosys](concepts/synthesis.md#installing-yosys).

## Install Into A Project With `uv`

Add `rtl_buddy` to your project environment:

```bash
uv add rtl_buddy
```

Then verify the install:

```bash
uv run rb --version
```

## Updating

To move a project to a newer `rtl_buddy` version:

```bash
uv add rtl_buddy@latest
uv sync
```

Commit the resulting lockfile change in your project repo.

## Installing A Pre-release

Pre-release versions follow PEP 440 (`2.3.0rc1`, `2.3.0rc2`, …). They are published to PyPI but excluded from the default resolver — an unqualified range like `>=2.2.0` will not pull one in.

To install a specific pre-release, pin it exactly:

```bash
uv add "rtl_buddy==2.3.0rc1"
```

Or in `pyproject.toml`:

```toml
dependencies = ["rtl_buddy==2.3.0rc1"]
```

This works without any `--pre` flag because the exact version is specified.

## Set Up The Agent Skill

`rtl_buddy` ships an agent skill for Claude Code and Codex. After installing `rtl_buddy`, run once per machine:

```bash
uv run rb skill install
```

This writes `SKILL.md` to `~/.claude/skills/rtl_buddy/` and `~/.codex/skills/rtl_buddy/`. Agents pick it up automatically. Re-run after upgrading `rtl_buddy` to refresh the content.

To install at project scope instead (overrides the user-level copy for that project):

```bash
uv run rb skill install --project
```

See [For Agents](agents.md) for scope semantics and `.gitignore` guidance.
