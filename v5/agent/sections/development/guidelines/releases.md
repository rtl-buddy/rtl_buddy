## Releases

Stable releases are produced by merging to `main` with one of the `version/patch`, `version/minor`, or `version/major` labels.
Pre-releases are cut from feature branches by workflow dispatch and should not be produced by merging pre-release branches into `main`.

Docs publishing, PyPI publishing, and downstream template updates depend on that sequence.
Do not push a template pin for an unreleased `rtl_buddy` version.

Every `version/major` bump must ship a migration page at `docs/migrations/vN-to-vM.md`, added to the `mkdocs.yml` nav, before merge.
The page documents every breaking behavior change — moved outputs, changed defaults, removed or renamed config fields, and any contract that downstream projects or hook scripts depend on — and tells readers what to update.
A recurring failure mode is a silent contract change buried in a PR description; the migration page is where it must live so users and agents find it.
