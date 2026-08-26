## Auto-start on macOS (LaunchAgent)

On macOS, `rb hub install-launchagent` writes `~/Library/LaunchAgents/com.rtl-buddy.hub.plist` (with `RunAtLoad` + `KeepAlive`) and `launchctl load`s it, so the hub starts at login and restarts if it dies. The agent runs `rb hub start --foreground` from the project directory and routes stdout/stderr to `.rtl-buddy/hub.log`. `rb hub uninstall-launchagent` unloads and removes the plist. On non-macOS platforms both commands error with `LaunchAgentUnsupportedError`.
