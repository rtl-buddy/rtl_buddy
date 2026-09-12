# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Structured physical metrics: the model, its artefact manifest, the readers.

`rb synth` and `rb power` both know far more than they report. Yosys
computes a per-module cell count and area on its way to the design total
the log prints; OpenSTA's session can attribute leakage, internal and
switching power to every leaf instance it just summed into one `Total`
row. Historically both were thrown away at the boundary: four scalars
survived into `SynthPassResults`, four more into `PowerPassResults`, and
"which module is the area" or "which instance is the leakage" meant
re-running the flow by hand with a different script.

This package is the other half of that pipeline, built to the shape the
coverage tier settled on in #397:

* :mod:`~rtl_buddy.phys.reports` — the tool-output readers. Yosys'
  ``stat -json`` dump and OpenSTA's per-instance ``report_power`` text,
  parsed into rows. Nothing here touches the filesystem layout.
* :mod:`~rtl_buddy.phys.model` — the versioned, backend-agnostic
  physical model: per module, per instance, plus the design totals the
  flows already parse as a sanity block.
* :mod:`~rtl_buddy.phys.manifest` — ``phys-manifest.json``, the
  discovery contract that says which artefacts a run produced.
* :mod:`~rtl_buddy.phys.publish` — the one entry point the tool backends
  call. It swallows its own failures on purpose: a model is a *by-product*
  of a synthesis or a power analysis, never a reason to fail one.
"""
