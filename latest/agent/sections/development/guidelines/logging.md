## Logging

Send runtime events through `log_event()` in `logging_utils.py`; do not call `logger.info()` directly. Human mode renders readable text and machine mode writes JSON Lines.

When adding events:

- Use dotted names such as `compile.start`, `sim.timeout`, or `suite_config.load_failed`.
- Include structured fields that are stable and useful for agents.
- Add a dedicated human-message case for WARNING or ERROR events.
- Use `log_console_event()` only for default-verbosity liveness signals or output previously visible on stdout, such as captured hook `print()` calls.
- Keep DEBUG and INFO events concise enough for machine logs.
