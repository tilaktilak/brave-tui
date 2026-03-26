"""
Generic GTK system tray icon for Brave-backed TUI apps.

Left-click  → opens a terminal running the app
Right-click → menu: Open / Restart daemon / Quit
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

class Tray:
    """
    System tray icon that manages a Brave-backed daemon.

    Args:
        app_id:      AppIndicator indicator ID (must be unique per app).
        app_name:    Human-readable name shown in menu labels.
        open_cmd:    Command list to run the TUI, e.g. ["python", "-m", "myapp"].
                     This is wrapped in a terminal emulator automatically.
        daemon_cmd:  Command list to start the daemon,
                     e.g. ["python", "-m", "myapp", "--daemon"].
        socket_path: Path to the daemon's Unix socket.
        pid_path:    Path to the daemon's PID file.
        icon_names:  GTK theme icon names to try in order of preference.
    """

    def __init__(
        self,
        app_id: str,
        app_name: str,
        open_cmd: list[str],
        daemon_cmd: list[str],
        socket_path: Path,
        pid_path: Path,
        icon_names: list[str],
    ) -> None:
        self._app_id = app_id
        self._app_name = app_name
        self._open_cmd = open_cmd
        self._daemon_cmd = daemon_cmd
        self._socket_path = socket_path
        self._pid_path = pid_path
        self._icon_names = icon_names
        self._Gtk = None
        self._AppIndicator3 = None

    def run(self) -> None:
        """Start the tray icon, launching the daemon first if needed."""
        import gi
        gi.require_version("AppIndicator3", "0.1")
        gi.require_version("Gtk", "3.0")
        from gi.repository import AppIndicator3, Gtk
        self._Gtk = Gtk
        self._AppIndicator3 = AppIndicator3

        from brave_tui.daemon import is_daemon_running
        if not is_daemon_running(self._pid_path):
            subprocess.Popen(
                self._daemon_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        theme = Gtk.IconTheme.get_default()
        icon_name = next(
            (n for n in self._icon_names if theme.has_icon(n)),
            "application-x-executable",
        )

        indicator = AppIndicator3.Indicator.new(
            self._app_id,
            icon_name,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        indicator.set_menu(self._build_menu())

        Gtk.main()

    # ------------------------------------------------------------------ menu

    def _build_menu(self):
        Gtk = self._Gtk
        menu = Gtk.Menu()

        item_open = Gtk.MenuItem(label=f"Open {self._app_name}")
        item_open.connect("activate", self._open_tui)
        menu.append(item_open)

        item_restart = Gtk.MenuItem(label="Restart daemon")
        item_restart.connect("activate", self._restart_daemon)
        menu.append(item_restart)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", self._quit)
        menu.append(item_quit)

        menu.show_all()
        return menu

    # ------------------------------------------------------------------ actions

    def _open_tui(self, *_) -> None:
        try:
            subprocess.Popen(self._make_terminal_cmd())
        except Exception as e:
            print(f"[tray] failed to open terminal: {e}", flush=True)

    def _restart_daemon(self, *_) -> None:
        def _do() -> None:
            self._kill_daemon()
            for _ in range(20):
                if not self._pid_path.exists() and not self._socket_path.exists():
                    break
                time.sleep(0.3)
            subprocess.Popen(
                self._daemon_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        threading.Thread(target=_do, daemon=True).start()

    def _quit(self, *_) -> None:
        self._kill_daemon()
        self._Gtk.main_quit()

    def _kill_daemon(self) -> None:
        """Try graceful IPC shutdown first, fall back to SIGTERM."""
        if self._socket_path.exists():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect(str(self._socket_path))
                sock.sendall(json.dumps({"cmd": "shutdown"}).encode() + b"\n")
                sock.close()
                return
            except Exception:
                pass
        if self._pid_path.exists():
            try:
                pid = int(self._pid_path.read_text().strip())
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass

    # ------------------------------------------------------------------ terminal detection

    def _make_terminal_cmd(self) -> list[str]:
        """Return a command that opens a terminal and runs self._open_cmd."""
        inner = " ".join(self._open_cmd)
        env_term = os.environ.get("TERMINAL", "")
        candidates = ([env_term] if env_term else []) + [
            "kitty", "alacritty", "wezterm", "foot",
            "gnome-terminal", "xfce4-terminal", "xterm",
        ]
        for term in candidates:
            if not shutil.which(term):
                continue
            match term:
                case "kitty":          return ["kitty", "--", "sh", "-c", inner]
                case "alacritty":      return ["alacritty", "-e", "sh", "-c", inner]
                case "wezterm":        return ["wezterm", "start", "--", "sh", "-c", inner]
                case "foot":           return ["foot", "sh", "-c", inner]
                case "gnome-terminal": return ["gnome-terminal", "--", "sh", "-c", inner]
                case "xfce4-terminal": return ["xfce4-terminal", "-e", inner]
                case _:                return [term, "-e", inner]
        return ["xterm", "-e", inner]
