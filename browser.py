"""
Generic Brave browser base class for web-app TUI backends.

Handles: Xvfb virtual display, Brave profile sync, Playwright launch.
Subclasses override _on_started() to do app-specific page setup.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path


def _find_brave() -> str:
    """Return the path to the Brave browser executable, or raise RuntimeError."""
    if env := os.environ.get("BRAVE_PATH"):
        return env
    candidates = [
        "/usr/bin/brave-browser",
        "/usr/bin/brave",
        "/usr/local/bin/brave-browser",
        "/usr/local/bin/brave",
        "/opt/brave.com/brave/brave",
        "/opt/brave/brave",
        # Flatpak
        "/var/lib/flatpak/exports/bin/com.brave.Browser",
        os.path.expanduser("~/.local/share/flatpak/exports/bin/com.brave.Browser"),
        # Snap
        "/snap/bin/brave",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    if found := shutil.which("brave-browser") or shutil.which("brave"):
        return found
    raise RuntimeError(
        "Brave browser not found. Install it from https://brave.com/linux/ "
        "or set the BRAVE_PATH environment variable."
    )


# The real Brave profile on the host system (never modified, only read).
REAL_PROFILE_DIR = Path.home() / ".config" / "BraveSoftware" / "Brave-Browser"


class BaseBraveBrowser:
    """
    Base class for Brave-backed web-app automation.

    Manages Xvfb, Brave profile sync from the user's real Brave installation,
    and the Playwright persistent browser context.

    Subclasses must override _on_started() to navigate to the target URL,
    select the right page, and do any app-specific initialisation.

    Args:
        profile_dir:  Working browser-profile directory (created automatically).
        extra_args:   Additional Chromium flags to pass at launch.
        ignore_args:  Default Playwright flags to suppress at launch.
    """

    def __init__(
        self,
        profile_dir: Path,
        extra_args: list[str] | None = None,
        ignore_args: list[str] | None = None,
    ) -> None:
        self._profile_dir = profile_dir
        self._extra_args: list[str] = extra_args or []
        self._ignore_args: list[str] = ignore_args or []
        self._playwright = None
        self._context = None
        self._page = None
        self._xvfb: subprocess.Popen | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start Xvfb, sync profile, launch Brave, then call _on_started()."""
        from playwright.async_api import async_playwright

        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._sync_profile()
        self._remove_stale_locks(self._profile_dir)
        self._start_xvfb()

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._profile_dir),
            headless=False,
            executable_path=_find_brave(),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-default-apps",
                "--autoplay-policy=no-user-gesture-required",
                "--no-sandbox",
                "--restore-last-session",
                *self._extra_args,
            ],
            ignore_default_args=[
                "--enable-automation",
                "--disable-extensions",
                "--disable-component-extensions-with-background-pages",
                "--disable-component-update",
                *self._ignore_args,
            ],
        )

        # Wait briefly so Brave can restore the previous session before we inspect pages.
        await asyncio.sleep(2)

        await self._on_started()

    async def _on_started(self) -> None:
        """
        Called once the browser context is ready and session tabs have loaded.

        Override this in your subclass to: select (or navigate to) the target
        page, set timeouts, open auxiliary pages, and do any app-specific setup.
        self._context is populated; self._page starts as None and should be set
        here by the subclass.
        """

    async def close(self) -> None:
        """Close the browser context and stop Playwright + Xvfb."""
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
        if self._xvfb:
            self._xvfb.terminate()

    # ------------------------------------------------------------------ internal helpers

    def _sync_profile(self) -> None:
        """
        Copy the Default directory from the real Brave profile to this app's profile.

        Only runs when real Brave is not open (avoids corrupting live files).
        Skips lock files, caches, and other volatile entries.
        """
        if not REAL_PROFILE_DIR.exists():
            return
        lock = REAL_PROFILE_DIR / "SingletonLock"
        if lock.exists():
            try:
                pid = int(os.readlink(lock).split("-")[-1])
                os.kill(pid, 0)
                return  # Brave is running — don't touch its files
            except (ValueError, OSError):
                pass  # stale lock, safe to proceed

        src = REAL_PROFILE_DIR / "Default"
        dst = self._profile_dir / "Default"
        if not src.exists():
            return

        skip = {
            "SingletonLock", "SingletonCookie", "SingletonSocket",
            "lockfile", "LOCK", "LOG", "LOG.old",
        }
        skip_dirs = {"GPUCache", "Code Cache", "DawnGraphiteCache", "DawnWebGPUCache"}

        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            if item.name in skip or item.name in skip_dirs:
                continue
            d = dst / item.name
            try:
                if item.is_dir():
                    if d.exists():
                        shutil.rmtree(d)
                    shutil.copytree(item, d)
                else:
                    shutil.copy2(item, d)
            except Exception:
                pass

    def _remove_stale_locks(self, profile: Path) -> None:
        """Remove SingletonLock only if the owning process is no longer alive."""
        lock = profile / "SingletonLock"
        if not lock.exists():
            return
        try:
            target = os.readlink(lock)
            pid = int(target.split("-")[-1])
            os.kill(pid, 0)
            raise RuntimeError(
                f"Brave is already running (pid {pid}). "
                "Close it before starting this app."
            )
        except (ValueError, OSError):
            for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                (profile / name).unlink(missing_ok=True)

    def _start_xvfb(self) -> None:
        """Launch Xvfb on a free display number and set the DISPLAY env var."""
        try:
            r_fd, w_fd = os.pipe()
            self._xvfb = subprocess.Popen(
                ["Xvfb", "-displayfd", str(w_fd), "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(w_fd,),
            )
            os.close(w_fd)
            display_num = os.read(r_fd, 16).decode().strip()
            os.close(r_fd)
            os.environ["DISPLAY"] = f":{display_num}"
        except FileNotFoundError:
            pass  # Xvfb not installed — assume a real display is available
