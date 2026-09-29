# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Structured physical metrics from `rb synth` and `rb power`.

The package holds a versioned per-module and per-instance model, a
manifest that names the artefacts a run produced, and the readers and
publishers around them. Submodules: `reports` (tool-output parsers),
`model`, `manifest`, `provenance` (run identity), `publish` (the entry
point the tool backends call) and `query` (the `rb phys` read verbs).
"""
