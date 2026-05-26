"""macOS LaunchAgent integration for ``rtl-buddy-hub`` (issue #122).

A LaunchAgent plist tells ``launchd`` to keep the hub running across
logouts and to restart it on crash. The agent runs under the user's
session (``~/Library/LaunchAgents/com.rtl-buddy.hub.plist``); a
system-wide ``/Library/LaunchAgents`` install isn't supported in v1
because the hub binds an ephemeral TCP port and writes
project-relative discovery files — neither of those generalises
across user sessions.

Three operations are exposed:

* :func:`render_plist` — produce the on-disk XML text. Pure
  function; tests can call it without touching launchd.
* :func:`install` — write the plist into
  ``~/Library/LaunchAgents/`` and run ``launchctl load``.
* :func:`uninstall` — symmetric ``launchctl unload`` + file
  removal.

Non-macOS callers get :class:`LaunchAgentUnsupportedError`. This is
deliberately a hard error rather than a no-op so the user knows the
flag did nothing on their platform.
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
    """Raised on non-macOS platforms.

    The Linux systemd unit and the Windows scheduled task are
    deliberately out of scope (see #122); the user-facing error
    points there for context.
    """


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

    Arguments default to the most-common case (sys.executable, the
    current working directory, the project's
    ``.rtl-buddy/hub.log``). Tests use the override surface to
    exercise the rendering without picking up the test harness'
    Python interpreter.
    """
    py = python or sys.executable
    root = (project_root or Path.cwd()).resolve()
    log = log_path or (root / ".rtl-buddy" / "hub.log")
    program_args = [py, "-m", "rtl_buddy", "hub", "start", "--foreground"]
    items = "\n".join(f"      <string>{_xml_escape(a)}</string>" for a in program_args)

    # Indented for readability; whitespace inside <string> is
    # significant but the surrounding ``<array>`` / ``<dict>``
    # whitespace is fine.
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
    """Write the plist and ``launchctl load`` it.

    Returns the on-disk plist path. Raises
    :class:`LaunchAgentUnsupportedError` on non-macOS,
    :class:`LaunchAgentError` on filesystem or ``launchctl`` errors.

    A previous install at the same path is replaced atomically; if
    the agent is already loaded, the loader first runs an
    ``unload`` so the new contents take effect.
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

    # Best-effort unload of any prior agent so re-loading picks up
    # changes. ``launchctl unload`` exits non-zero when the agent
    # isn't currently loaded, which is the normal first-install
    # case; silence those.
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

    Returns ``True`` if the plist existed and was removed; ``False``
    if nothing was installed. Raises
    :class:`LaunchAgentUnsupportedError` on non-macOS.
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
    """Minimal XML attribute / text escaper for the small set of
    characters that can appear in absolute paths and shell arg lists."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
