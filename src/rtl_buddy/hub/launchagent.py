"""macOS LaunchAgent integration for ``rtl-buddy-hub``.

A per-user LaunchAgent plist (``~/Library/LaunchAgents/com.rtl-buddy.hub.plist``) makes ``launchd`` keep the hub running across logouts and restart it on a crash. :func:`render_plist` builds the XML, :func:`install` writes it and runs ``launchctl load``, and :func:`uninstall` unloads and removes it. System-wide ``/Library/LaunchAgents`` installs are not supported, because the hub binds an ephemeral port and writes project-relative discovery files.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.rtl-buddy.hub"
PLIST_FILENAME = f"{LABEL}.plist"


class LaunchAgentError(Exception):
    """Raised when installing or uninstalling the LaunchAgent fails."""


class LaunchAgentUnsupportedError(LaunchAgentError):
    """Raised on non-macOS platforms; there is no systemd or scheduled-task equivalent."""


def is_supported() -> bool:
    """``True`` on macOS, ``False`` everywhere else."""
    return sys.platform == "darwin"


def default_plist_path() -> Path:
    """``~/Library/LaunchAgents/com.rtl-buddy.hub.plist``."""
    return Path.home() / "Library" / "LaunchAgents" / PLIST_FILENAME


def render_plist(
    *,
    python: str | None = None,
    project_root: Path | None = None,
    log_path: Path | None = None,
) -> str:
    """Build the LaunchAgent plist XML.

    Arguments default to ``sys.executable``, the current directory and
    ``<root>/.rtl-buddy/hub.log``; tests override them.
    """
    py = python or sys.executable
    root = (project_root or Path.cwd()).resolve()
    log = log_path or (root / ".rtl-buddy" / "hub.log")
    program_args = [py, "-m", "rtl_buddy", "hub", "start", "--foreground"]
    items = "\n".join(f"      <string>{_xml_escape(a)}</string>" for a in program_args)

    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{items}
  </array>
  <key>WorkingDirectory</key>
  <string>{_xml_escape(str(root))}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{_xml_escape(str(log))}</string>
  <key>StandardErrorPath</key>
  <string>{_xml_escape(str(log))}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
  </dict>
</dict>
</plist>
"""


def install(
    *,
    python: str | None = None,
    project_root: Path | None = None,
    log_path: Path | None = None,
    plist_path: Path | None = None,
    launchctl: str = "launchctl",
) -> Path:
    """Write the plist and ``launchctl load`` it; return the plist path.

    Raises :class:`LaunchAgentUnsupportedError` off macOS and
    :class:`LaunchAgentError` on filesystem or ``launchctl`` failures. An
    existing agent is unloaded first so the new contents take effect.
    """
    if not is_supported():
        raise LaunchAgentUnsupportedError(
            f"LaunchAgent install is macOS-only; current platform is "
            f"{platform.system()!r}. See issue #122 for the systemd / "
            f"scheduled-task scope decision."
        )
    target = plist_path or default_plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    xml = render_plist(python=python, project_root=project_root, log_path=log_path)

    # unload exits non-zero when the agent is not loaded (normal on first install).
    if target.exists() and shutil.which(launchctl) is not None:
        subprocess.run(
            [launchctl, "unload", str(target)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    target.write_text(xml)

    if shutil.which(launchctl) is None:
        raise LaunchAgentError(
            f"{launchctl!r} not found on PATH; wrote the plist to {target} "
            f"but couldn't load it. Run `launchctl load {target}` manually."
        )
    result = subprocess.run(
        [launchctl, "load", str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise LaunchAgentError(
            f"launchctl load {target} failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return target


def uninstall(
    *,
    plist_path: Path | None = None,
    launchctl: str = "launchctl",
) -> bool:
    """``launchctl unload`` and delete the plist.

    Returns whether a plist was removed. Raises
    :class:`LaunchAgentUnsupportedError` off macOS.
    """
    if not is_supported():
        raise LaunchAgentUnsupportedError(
            f"LaunchAgent uninstall is macOS-only; current platform is "
            f"{platform.system()!r}."
        )
    target = plist_path or default_plist_path()
    if not target.exists():
        return False
    if shutil.which(launchctl) is not None:
        subprocess.run(
            [launchctl, "unload", str(target)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    try:
        target.unlink()
    except OSError as exc:
        raise LaunchAgentError(f"could not remove {target}: {exc}") from exc
    return True


def _xml_escape(value: str) -> str:
    """Escape the five XML special characters."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
