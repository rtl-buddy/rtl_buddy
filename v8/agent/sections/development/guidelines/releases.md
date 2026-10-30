## Releases

Merge stable releases to `main` with one `version/patch`, `version/minor`, or `version/major` label. Cut prereleases from feature branches by workflow dispatch. Do not merge a prerelease branch to release it or push a downstream pin before the PyPI release exists.

A `version/major` PR must add one `## vN to vM` section to `docs/migrations.md` covering every moved output, changed default, removed or renamed field, and downstream contract change.
