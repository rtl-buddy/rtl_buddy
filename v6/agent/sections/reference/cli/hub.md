## hub

```text
Usage: rtl-buddy hub [OPTIONS] COMMAND [ARGS]...

 manage the rtl-buddy-hub daemon

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ start                  start the rtl-buddy-hub daemon for this project               │
│ stop                   ask the running hub to shut down                              │
│ status                 print the running hub's discovery record                      │
│ log                    tail the hub log                                              │
│ install-launchagent    install the macOS LaunchAgent so the hub auto-starts at login │
│ uninstall-launchagent  remove the macOS LaunchAgent                                  │
│ config                 hub.toml utilities                                            │
│ send                   One-shot peer for the running rtl-buddy-hub. Connects as      │
│                        origin=cli.                                                   │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
