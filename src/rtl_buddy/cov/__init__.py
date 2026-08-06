# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Structured coverage: the model, its artefact manifest, and the read verbs.

A simulator run leaves coverage on disk as a pile of loosely related
files — raw databases, LCOV exports, per-type ``.info`` files, HTML
trees, Coverview zips. Historically every boundary between those files
and a consumer threw the structure away: four scalars survived into
``--machine``, artefact paths survived only as display strings, and
re-reading last night's coverage meant re-running the regression.

This package is the other half of that pipeline:

* :mod:`~rtl_buddy.cov.source_paths` — the one resolver that maps a
  simulator-recorded source path to a real file in the project. It used
  to exist three times with three behaviours.
* :mod:`~rtl_buddy.cov.raw` — the raw Verilator database reader. Every
  record type, not just ``t=user``: toggle and expression detail only
  survives here, since ``verilator_coverage --write-info`` folds both
  into anonymous ``DA:`` records.
* :mod:`~rtl_buddy.cov.model` — the versioned, simulator-agnostic
  coverage model: per file, per point, per test.
* :mod:`~rtl_buddy.cov.manifest` — ``cov_dir/manifest.json``, the
  discovery contract that says which artefacts a run produced.
* :mod:`~rtl_buddy.cov.query` — the payload builders behind
  ``rb cov summary`` and ``rb cov module``. Plain functions, importable
  without the CLI, so the MCP tools can wrap them verbatim.
"""
