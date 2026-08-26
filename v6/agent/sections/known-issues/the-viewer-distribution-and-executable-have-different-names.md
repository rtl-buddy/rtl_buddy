## The viewer distribution and executable have different names

Install the `rtl-buddy-sch` distribution; rtl_buddy invokes its `rtl-buddy-view` executable and imports `rtl_buddy_view`:

```bash
uv tool install rtl-buddy-sch
```

`rb tool-check --explain rtl-buddy-sch` accepts the alias but reports the canonical tool key `rtl-buddy-view`.
