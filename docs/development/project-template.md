---
description: Authoring and review guidelines for keeping rtl-buddy-project-template aligned with user-visible rtl_buddy behavior.
---

# Project Template Guidelines

These guidelines apply when adding examples to
[`rtl-buddy-project-template`](https://github.com/rtl-buddy/rtl-buddy-project-template)
and when checking whether it keeps pace with `rtl_buddy`. The template should
make user-visible behavior understandable, runnable, and easy to adapt.

## Choose The Example Role

Classify an example before deciding where and how to add it:

- A **sandbox flow** belongs to the integrated reference design. It should join
  the same DUT, spec, model, test, and regression story rather than form a
  disconnected side demo.
- A **template demo** is a small, isolated example of one capability. It may be
  separate from the sandbox, but it should remain focused and readable.

Do not place a disconnected template demo in the sandbox or let a template
demo grow too large and entangled to work as a reference.

## Author Examples

Every example should be:

- **Isolated:** easy to locate and understand without unrelated material.
- **Integrated or minimal:** connected to the sandbox flow, or deliberately
  small as a template demo.
- **Explained:** accompanied by a comment or documentation that says what it
  does and when to use it.
- **Runnable:** includes a concrete `uv run rb ...` command.

The quality bar is that an RTL engineer new to `rtl_buddy` can find the example
and immediately understand how to run and adapt it.

## Keep The Template Current

Treat template maintenance as a delta from `rtl_buddy`, not a static checklist.
Compare from the last `rtl_buddy` dependency bump in the template through the
current change. For targeted work, use the specified change range and the
latest template `main` as the downstream baseline.

Inspect user-visible changes to:

- CLI commands, flags, or options in `src/rtl_buddy/rtl_buddy.py` and
  `docs/reference/cli.md`.
- YAML fields or config sections in `src/rtl_buddy/config/` and
  `docs/reference/yaml.md`.
- User workflows in `docs/concepts/`.
- Plugin behavior in `docs/concepts/plugins.md`.
- Pass/fail behavior in `src/rtl_buddy/tools/`.

Internal refactors that do not change behavior or configuration need no
template update.

## Review Template Coverage

Search the template configuration, RTL, plugins, and explanatory material for
each relevant behavior. Verify the example role and every authoring rule above.
An absent user-visible feature is a gap.

For a full review, use this table:

| Feature / change | `rtl_buddy` commit | Intended role | Template location | Isolated | Integrated/minimal | Explained | Runnable | Action needed |
|---|---|---|---|---|---|---|---|---|

Mark evaluation cells with ✅, ❌, or ⚠️. Follow the table with a **Gap
Summary** containing one recommended action for each ❌ or ⚠️ row. Follow
[Code Reviews](reviews.md) for pull request scope and feedback rules.
