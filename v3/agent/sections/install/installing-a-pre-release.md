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
