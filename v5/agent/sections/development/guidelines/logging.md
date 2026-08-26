## Logging

All runtime logging goes through `log_event()` in `src/rtl_buddy/logging_utils.py`.
Do not use direct `logger.info(f"...")` calls for runtime events.

Human mode converts events into readable text for `rtl_buddy.log` and console output.
Machine mode writes JSON Lines with the event name, fields, and human message.

When adding events:

- Use dotted names such as `compile.start`, `sim.timeout`, or `suite_config.load_failed`.
- Include structured fields that are stable and useful for agents.
- Add a dedicated human-message case for WARNING or ERROR events.
- Keep DEBUG and INFO events concise enough for machine logs.
