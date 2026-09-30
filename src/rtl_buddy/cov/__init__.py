# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Structured coverage: the model, its artefact manifest, and the read verbs.

* :mod:`~rtl_buddy.cov.source_paths` maps a simulator-recorded source path to a project file.
* :mod:`~rtl_buddy.cov.raw` reads Verilator's raw coverage database, including toggle and expression detail that ``--write-info`` loses.
* :mod:`~rtl_buddy.cov.model` is the versioned, simulator-agnostic coverage model.
* :mod:`~rtl_buddy.cov.manifest` is ``cov_dir/manifest.json``, which lists a run's artefacts.
* :mod:`~rtl_buddy.cov.query` builds the payloads behind ``rb cov summary`` and ``rb cov module``; the MCP tools wrap them.
"""
