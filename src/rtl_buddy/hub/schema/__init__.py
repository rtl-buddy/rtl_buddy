"""Vendored copy of the hub-protocol-v1 JSON Schema.

Source of truth: ``schemas/hub-protocol-v1.json`` in
``rtl-buddy/rtl-buddy-sch`` (the repo formerly named ``rtl-buddy-view``;
frozen by Phase 10a, ``rtl-buddy/rtl-buddy-sch#19``). The file in this
package MUST stay byte-identical to that source; the
``tests/test_hub_protocol.py::test_vendored_schema_matches_source_when_view_repo_present``
case enforces this when that repo is checked out alongside this one, and
``::test_origin_enum_matches_vendored_schema`` pins :class:`Origin` to
this copy's ``origin`` enum.

Adding an origin is a lockstep edit across three repos; the checklist is
``docs/hub-protocol.md`` section 13 in rtl-buddy-sch.
"""
