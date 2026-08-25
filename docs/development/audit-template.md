---
description: How to audit rtl-buddy-project-template against user-visible rtl_buddy changes and report downstream gaps.
---

# Audit The Project Template

This audit checks whether
[`rtl-buddy-project-template`](https://github.com/rtl-buddy/rtl-buddy-project-template)
keeps pace with user-visible changes in `rtl_buddy`. The audit is a delta, not a
static checklist: determine what changed, then assess whether the template makes
that behavior understandable and runnable.

## Choose The Change Window

Compare from the last `rtl_buddy` dependency bump in the template through the
current `rtl_buddy` change. For a targeted audit, use the specified change range
and the latest template `main` as the downstream baseline.

Inspect each relevant `rtl_buddy` change for:

- CLI commands, flags, or options in `src/rtl_buddy/rtl_buddy.py` and
  `docs/reference/cli.md`.
- YAML fields or config sections in `src/rtl_buddy/config/` and
  `docs/reference/yaml.md`.
- User workflows in `docs/concepts/`.
- Plugin behavior in `docs/concepts/plugins.md`.
- Pass/fail behavior in `src/rtl_buddy/tools/`.

Skip internal refactors that do not change behavior or configuration.

## Check The Template

Search the template for each user-visible change in configuration, RTL,
plugins, and explanatory material. Classify each example before judging it:

- A **sandbox flow** belongs to the integrated reference design. It should join
  the same DUT, spec, model, test, and regression story rather than form a
  disconnected side demo.
- A **template demo** is a small, isolated example of one capability. It may be
  separate from the sandbox, but it should remain focused and readable.

Assess whether every example is:

- **Isolated:** easy to locate and understand without unrelated material.
- **Integrated or minimal:** connected to the sandbox flow, or deliberately
  small as a template demo.
- **Explained:** accompanied by a comment or documentation that says what it
  does and when to use it.
- **Runnable:** includes a concrete `uv run rb ...` command.

An absent feature is a gap. Also flag sandbox examples that are disconnected
and template demos that are too large or entangled to work as references.

## Report The Audit

For a full audit, use this table:

| Feature / change | `rtl_buddy` commit | Intended role | Template location | Isolated | Integrated/minimal | Explained | Runnable | Action needed |
|---|---|---|---|---|---|---|---|---|

Mark evaluation cells with ✅, ❌, or ⚠️. Follow the table with a **Gap
Summary** containing one recommended action for each ❌ or ⚠️ row.

The quality bar is that an RTL engineer new to `rtl_buddy` can find the example
and immediately understand how to run and adapt it. Follow
[Code Reviews](reviews.md) for pull request scope and feedback rules.
