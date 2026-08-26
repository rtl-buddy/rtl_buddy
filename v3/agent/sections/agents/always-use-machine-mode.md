## Always use machine mode

Run `rtl_buddy` with `--machine` in all agent-driven workflows:

```bash
rtl-buddy --machine test basic
rtl-buddy --machine regression -c design/regression.yaml
```

In machine mode:

- `rtl_buddy.log` is written as **JSON Lines** (one JSON object per line) instead of human-readable text.
- Console output switches to plain, colorless text — no Rich formatting, no spinners.
- All structured event fields (event name, status, durations, paths) are present in the log.

This makes it reliable to parse outcomes from `rtl_buddy.log` without screen-scraping.
