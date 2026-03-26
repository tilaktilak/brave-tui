# brave-tui

A Python library for building terminal UIs backed by a Brave browser.

Handles the infrastructure so you can focus on your app: Brave is launched in a
virtual display (Xvfb), your existing Brave profile is synced so sessions carry
over, and a background daemon exposes the browser over a Unix socket that your
TUI connects to.

## How it works

```
Tray (GTK)
  └── Daemon                   background process
        ├── BaseBraveBrowser   Brave + Playwright + Xvfb
        └── Unix socket        ~/.config/<app>/daemon.sock
              └── BraveClient  thin IPC proxy used by the TUI
```

## Requirements

- Python 3.10+
- [Playwright](https://playwright.dev/python/) — `pip install playwright && playwright install chromium`
- [Brave browser](https://brave.com/linux/)
- `Xvfb` — `sudo apt install xvfb`
- `libayatana-appindicator3` (tray only) — `sudo apt install gir1.2-ayatana-appindicator3-0.1 python3-gi`

## Installation

```bash
pip install brave-tui
```

Or as a git submodule inside your project:

```bash
git submodule add git@github.com:tilaktilak/brave-tui.git brave_tui
```

## Usage

### 1. Subclass `BaseBraveBrowser`

Implement `_on_started()` to navigate to your web app and do any page setup.
Add one method per action you want to expose — the daemon will dispatch to them
automatically.

```python
from pathlib import Path
from brave_tui import BaseBraveBrowser

class MyAppBrowser(BaseBraveBrowser):
    URL = "https://example.com"

    def __init__(self):
        super().__init__(profile_dir=Path.home() / ".config" / "myapp" / "browser-profile")

    async def _on_started(self) -> None:
        # Pick (or open) the right tab after Brave restores the session
        pages = self._context.pages
        self._page = next((p for p in pages if self.URL in p.url), None)
        if self._page is None:
            self._page = await self._context.new_page()
            await self._page.goto(self.URL, wait_until="domcontentloaded")
        self._page.set_default_timeout(5000)

    # Any public method becomes a callable daemon command
    async def get_title(self) -> str:
        return await self._page.title()

    async def click_button(self, label: str) -> None:
        await self._page.click(f"button:has-text('{label}')")
```

### 2. Start the `Daemon`

```python
import asyncio
from pathlib import Path
from brave_tui import Daemon

SOCKET = Path.home() / ".config" / "myapp" / "daemon.sock"
PID    = Path.home() / ".config" / "myapp" / "daemon.pid"

asyncio.run(Daemon(MyAppBrowser(), SOCKET, PID).run())
```

The daemon binds the socket immediately so clients can queue up while the
browser is still loading.

### 3. Connect with `BraveClient`

```python
from brave_tui import BraveClient
from pathlib import Path

client = BraveClient(Path.home() / ".config" / "myapp" / "daemon.sock")
await client.start()

title = await client.get_title()          # proxied automatically via __getattr__
await client.click_button(label="Play")   # kwargs are forwarded as-is
await client.close()
```

Any method name that is not defined on `BraveClient` itself is forwarded to the
daemon as `{"cmd": "<name>", ...kwargs}`. Methods that return dataclasses need a
thin override to reconstruct the typed object from the dict the daemon sends
back:

```python
from brave_tui import BraveClient
from myapp.browser import MyResult  # your dataclass

class MyClient(BraveClient):
    async def search(self, query: str) -> list[MyResult]:
        return [MyResult(**i) for i in await self._call("search", query=query)]
```

### 4. Add a system tray (optional)

```python
import sys
from brave_tui import Tray
from pathlib import Path

Tray(
    app_id="myapp",
    app_name="My App",
    open_cmd=[sys.executable, "-m", "myapp"],
    daemon_cmd=[sys.executable, "-m", "myapp", "--daemon"],
    socket_path=Path.home() / ".config" / "myapp" / "daemon.sock",
    pid_path=Path.home() / ".config" / "myapp" / "daemon.pid",
    icon_names=["myapp", "application-x-executable"],
).run()
```

`Tray.run()` is blocking. It starts the daemon if it isn't running, shows the
tray icon, and handles Open / Restart daemon / Quit from the right-click menu.

The `gi` / AppIndicator3 import happens inside `run()`, so importing `Tray` at
the top of your module is safe even in environments where the GTK bindings are
not installed.

## IPC protocol

The daemon speaks newline-delimited JSON over a Unix socket:

```
→ {"cmd": "get_title"}\n
← {"result": "My App"}\n

→ {"cmd": "click_button", "label": "Play"}\n
← {"result": null}\n

→ {"cmd": "unknown"}\n
← {"error": "unknown command: 'unknown'"}\n
```

Built-in commands: `ping` → `{"result": "pong"}`, `shutdown` → graceful exit.

Dataclass return values are serialised to dicts automatically by the daemon.
Private methods (names starting with `_`) are not accessible over IPC.

## Real-world example

[yui](https://github.com/tilaktilak/yui) — a YouTube Music TUI built on brave-tui.
