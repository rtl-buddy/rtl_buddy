# rtl-buddy
# vim: set sw=4:ts=4:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""
nvim_install: install/update the unified rtl-buddy-nvim editor plugin.

Replaces the legacy ``rb wave-install-nvim`` flow that dropped a standalone
``rtl_buddy_wave.lua`` (annotation only, no hub). The annotation feature now
lives inside ``rtl-buddy-nvim`` alongside the hub adapter, so a single command
clones that one plugin into the nvim *pack* dir and writes a managed setup file
that auto-connects to the hub and renders ``rb wave`` annotations.

Delivery is a pinned ``git clone``: the ref below is known-compatible with this
rtl_buddy's hub protocol version. The hub also enforces the protocol version on
the wire (``hub/protocol.py::decode`` rejects a mismatched ``v``), so a stale
clone fails fast at handshake rather than silently misbehaving. Bump
:data:`RTL_BUDDY_NVIM_REF` whenever :data:`rtl_buddy.hub.protocol.PROTOCOL_VERSION`
changes.
"""

import logging
import os
import shutil
from importlib.metadata import version as _pkg_version
from pathlib import Path

from ..errors import FatalRtlBuddyError
from ..logging_utils import emit_console_text, log_event
from ..process_utils import run_managed_process

logger = logging.getLogger(__name__)

# The upstream plugin and the revision pinned to this rtl_buddy release. The
# ref is a git tag (or branch) accepted by ``git clone --branch``. Keep it in
# lockstep with hub/protocol.py::PROTOCOL_VERSION — see the module docstring.
RTL_BUDDY_NVIM_REPO = "https://github.com/rtl-buddy/rtl-buddy-nvim"
RTL_BUDDY_NVIM_REF = "v0.2.0"

# The hub wire-protocol version the pinned plugin speaks. The hub enforces it
# on the wire (``hub/protocol.py::decode`` rejects a mismatched ``v``), so a
# pin/protocol drift would only surface at a user's handshake — never at build
# time. When ``PROTOCOL_VERSION`` changes, tag a compatible rtl-buddy-nvim
# release, bump ``RTL_BUDDY_NVIM_REF`` to it, and bump this constant.
# ``test_pin_tracks_hub_protocol_version`` is the CI tripwire that fails if the
# two drift apart. See docs/known-issues.md.
_PIN_PROTOCOL_VERSION = 1

# Generous ceiling for the one-shot git clone/fetch; the plugin repo is tiny, so
# this only trips on a hung network — turning an indefinite hang into a clear
# error that points at ``--source <local path>``.
_GIT_TIMEOUT_S = 300.0

# Env overrides, mainly for offline/dev installs against a sibling checkout
# (``--source ../rtl-buddy-nvim --ref <branch>``) and for the test suite.
_ENV_SOURCE = "RTL_BUDDY_NVIM_SOURCE"
_ENV_REF = "RTL_BUDDY_NVIM_REF"


def _site_dir() -> Path:
    """``~/.local/share/nvim/site`` — recomputed per call so tests can repoint $HOME."""
    return Path(os.path.expanduser("~/.local/share/nvim/site"))


def pack_dir() -> Path:
    """Native-package install location for the plugin (auto-loaded by nvim)."""
    return _site_dir() / "pack" / "rtlbuddy" / "start" / "rtl-buddy-nvim"


def setup_file() -> Path:
    """Managed bootstrap that calls ``setup()`` — auto-sourced from ``site/plugin``."""
    return _site_dir() / "plugin" / "rtl_buddy_setup.lua"


def legacy_plugin_file() -> Path:
    """The pre-#272 standalone annotation plugin; removed on install if present."""
    return _site_dir() / "plugin" / "rtl_buddy_wave.lua"


def is_installed() -> bool:
    """True if the unified rtl-buddy-nvim plugin is present in the pack dir."""
    return pack_dir().exists()


def _rtl_buddy_version() -> str:
    try:
        return _pkg_version("rtl-buddy")
    except (
        Exception
    ):  # pragma: no cover - packaging metadata always present in practice
        return "unknown"


# ---------------------------------------------------------------------------
# managed setup file
# ---------------------------------------------------------------------------

_LSP_BLOCK = """
-- Auto-start verible-verilog-ls when it's on PATH and no LSP is already
-- attached, so :RtlBuddyShow resolves real declarations out of the box.
-- Guarded so it never clobbers a user-configured language server.
if vim.fn.executable("verible-verilog-ls") == 1 then
  vim.api.nvim_create_autocmd("FileType", {
    pattern = { "verilog", "systemverilog" },
    desc = "rtl-buddy: start verible-verilog-ls",
    callback = function(args)
      if #vim.lsp.get_clients({ bufnr = args.buf }) > 0 then
        return
      end
      vim.lsp.start({
        name = "verible",
        cmd = { "verible-verilog-ls" },
        root_dir = vim.fs.root(args.buf, { "root_config.yaml", ".git" }) or vim.fn.getcwd(),
      })
    end,
  })
end
"""


def _setup_file_contents(*, lsp: bool) -> str:
    header = (
        f"-- rtl-buddy managed setup — written by `rb nvim-install` "
        f"(rtl-buddy {_rtl_buddy_version()}, pin {RTL_BUDDY_NVIM_REF}).\n"
        "-- Do NOT edit by hand; re-run `rb nvim-install --force` to regenerate.\n"
        "-- To manage rtlbuddy yourself instead, set\n"
        "--   vim.g.rtl_buddy_no_managed_setup = true\n"
        "-- before this file loads (e.g. early in init.lua) and call\n"
        '--   require("rtlbuddy").setup({ ... })\n'
    )
    guard = (
        "if vim.g.rtl_buddy_no_managed_setup or vim.g.rtl_buddy_setup_done then\n"
        "  return\n"
        "end\n"
        "vim.g.rtl_buddy_setup_done = true\n"
    )
    body = (
        'local ok, rtlbuddy = pcall(require, "rtlbuddy")\n'
        "if not ok then\n"
        '  vim.notify("rtl-buddy: plugin not found on runtimepath — rerun `rb nvim-install`",'
        " vim.log.levels.WARN)\n"
        "  return\n"
        "end\n"
        "\n"
        "rtlbuddy.setup({\n"
        "  auto_connect = true,\n"
        "  use_lsp_for_symbol = true,\n"
        "  wave = { annotate = true },\n"
        "})\n"
    )
    parts = [header, "\n", guard, "\n", body]
    if lsp:
        parts.append(_LSP_BLOCK)
    return "".join(parts)


def _write_setup_file(*, lsp: bool) -> Path:
    path = setup_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_setup_file_contents(lsp=lsp))
    return path


def _remove_legacy() -> Path | None:
    legacy = legacy_plugin_file()
    if legacy.is_file():
        legacy.unlink()
        return legacy
    return None


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def _git(git: str, args: list[str], *, cwd: str) -> None:
    # Routed through run_managed_process (the repo convention for external tools)
    # so the networked clone gets an explicit cwd, a timeout, and process-group
    # cleanup on Ctrl-C — rather than a bare subprocess.run that could hang.
    cmd = [git, *args]
    log_event(logger, logging.DEBUG, "nvim_install.git", cmd=" ".join(cmd))
    result = run_managed_process(
        cmd, capture_output=True, text=True, cwd=cwd, timeout=_GIT_TIMEOUT_S
    )
    label = " ".join(args[:2])
    if result.timed_out:
        raise FatalRtlBuddyError(
            f"git {label} timed out after {_GIT_TIMEOUT_S:.0f}s — slow/hung remote? "
            "Retry, or install offline with `--source <local path>`."
        )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise FatalRtlBuddyError(
            f"git {label} failed (exit {result.returncode}): {stderr}"
        )


def _clone(git: str, source: str, ref: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # dest is absolute; run from its parent so the clone is explicitly rooted.
    _git(
        git,
        ["clone", "--depth", "1", "--branch", ref, source, str(dest)],
        cwd=str(dest.parent),
    )


def _update(git: str, source: str, ref: str, dest: Path) -> None:
    # Fetch the pinned ref from `source` explicitly (so --source on update can
    # re-point the origin) and hard-reset the managed clone to it.
    _git(git, ["-C", str(dest), "fetch", "--depth", "1", source, ref], cwd=str(dest))
    _git(git, ["-C", str(dest), "reset", "--hard", "FETCH_HEAD"], cwd=str(dest))


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def install(
    *,
    force: bool = False,
    update: bool = False,
    source: str | None = None,
    ref: str | None = None,
    lsp: bool = True,
) -> None:
    """Install or update the unified rtl-buddy-nvim plugin.

    ``force`` removes any existing install and re-clones. ``update`` syncs an
    existing clone to the pinned ref (re-cloning if the dir is not a git repo).
    ``source`` / ``ref`` (or the ``RTL_BUDDY_NVIM_SOURCE`` / ``RTL_BUDDY_NVIM_REF``
    env vars) override the upstream repo and pinned revision. ``lsp`` controls
    whether the managed setup file auto-starts ``verible-verilog-ls``.
    """
    git = shutil.which("git")
    if git is None:
        raise FatalRtlBuddyError(
            "`rb nvim-install` requires git on PATH to fetch the rtl-buddy-nvim plugin"
        )

    source = source or os.environ.get(_ENV_SOURCE) or RTL_BUDDY_NVIM_REPO
    ref = ref or os.environ.get(_ENV_REF) or RTL_BUDDY_NVIM_REF
    pack = pack_dir()

    cloned = False
    if pack.exists():
        if force:
            shutil.rmtree(pack)
            _clone(git, source, ref, pack)
            cloned = True
        elif update:
            if (pack / ".git").is_dir():
                _update(git, source, ref, pack)
            else:
                emit_console_text(f"{pack} is not a git checkout; re-cloning at {ref}.")
                shutil.rmtree(pack)
                _clone(git, source, ref, pack)
                cloned = True
        else:
            emit_console_text(
                f"Plugin already installed: {pack}\n"
                "  --update to sync to the pinned revision, --force to reinstall."
            )
    else:
        _clone(git, source, ref, pack)
        cloned = True

    # Always (re)write the managed setup file and clear the legacy plugin so a
    # plain re-run repairs a partial/old install.
    setup_path = _write_setup_file(lsp=lsp)
    removed_legacy = _remove_legacy()

    verb = "Installed" if cloned else "Refreshed"
    lines = [f"{verb} rtl-buddy-nvim at {pack}"]
    if source != RTL_BUDDY_NVIM_REPO:
        lines.append(f"  source: {source} (ref {ref})")
    else:
        lines.append(f"  ref: {ref}  (rtl-buddy {_rtl_buddy_version()})")
    lines.append(f"  managed setup: {setup_path}")
    if removed_legacy is not None:
        lines.append(f"  removed legacy annotation plugin: {removed_legacy}")
    lines.append("Restart nvim, then run `:checkhealth rtlbuddy`.")
    emit_console_text("\n".join(lines))
