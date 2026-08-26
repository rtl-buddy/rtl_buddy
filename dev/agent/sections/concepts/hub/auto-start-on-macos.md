## Auto-start on macOS

Install or remove the bundled LaunchAgent:

```bash
rb hub install-launchagent
rb hub uninstall-launchagent
```

The agent runs the hub from the project directory, restarts it when needed, and logs to `.rtl-buddy/hub.log`. These commands fail with `LaunchAgentUnsupportedError` on other platforms.
