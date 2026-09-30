## Auto-start on macOS

```bash
rb hub install-launchagent
rb hub uninstall-launchagent
```

The LaunchAgent runs the hub from the project directory, restarts it, and logs to `.rtl-buddy/hub.log`. Other platforms fail with `LaunchAgentUnsupportedError`.
