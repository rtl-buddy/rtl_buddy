## The viewer distribution and executable have different names

Install the `rtl-buddy-sch` distribution; rtl_buddy invokes its `rtl-buddy-view` executable and imports `rtl_buddy_view`:

```bash
uv tool install rtl-buddy-sch
```

`rb tool-check --explain rtl-buddy-sch` accepts the alias but reports the canonical tool key `rtl-buddy-view`.

`rb graph build` needs a newer viewer than the other viewer-backed commands: 0.4.0, against the shared 0.3.0 floor. `rb tool-check` reports the viewer itself as `ok` from 0.3.0 and marks only `rb graph` as `outdated` below 0.4.0; `rb tool-check --required-for graph` exits non-zero in that state. Releases before this distinction reported `rb graph` as ready on a 0.3.x viewer and then failed the design tier at build time.
