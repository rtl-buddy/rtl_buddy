## The viewer distribution and executable have different names

Install the `rtl-buddy-sch` distribution; rtl_buddy invokes its `rtl-buddy-view` executable:

```bash
uv tool install rtl-buddy-sch
```

`rb tool-check --explain rtl-buddy-sch` accepts the alias but reports the tool key `rtl-buddy-view`. `rb graph build` needs viewer 0.4.0; other viewer-backed commands need 0.3.0, and `rb tool-check` marks only `rb graph` as `outdated` below 0.4.0.
