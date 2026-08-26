## Machine mode log format

Each line in `rtl_buddy.log` (machine mode) is a JSON object:

```json
{"event": "sim.completed", "test": "smoke", "duration_sec": 4.2, "message": "smoke: simulation completed in 4.20s"}
{"event": "postproc.completed", "test": "smoke", "result": "PASS", "desc": "smoke completed", "message": "smoke: post-processing completed with result PASS (smoke completed)"}
```

Key fields:

- `event`: dotted event name identifying what happened (e.g. `sim.start`, `compile.failed`, `postproc.completed`)
- `message`: the human-readable message corresponding to the event
- Other fields are event-specific (name, duration, seed, exit code, etc.)
