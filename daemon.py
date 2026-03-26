"""
Generic daemon for Brave-backed TUI apps.

Starts the browser in the background, exposes it over a Unix socket using a
simple JSON-line request/response protocol, and dispatches commands dynamically
to methods on the browser instance.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import signal
from pathlib import Path

from brave_tui.browser import BaseBraveBrowser


def is_daemon_running(pid_path: Path) -> bool:
    """Return True if the daemon process recorded in pid_path is alive."""
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        return False


class Daemon:
    """
    Background process that owns a BaseBraveBrowser and serves IPC clients.

    Protocol: newline-delimited JSON.
      Request:  {"cmd": "<method_name>", "param1": value, ...}
      Response: {"result": <return_value>}  or  {"error": "<message>"}

    Built-in commands (not dispatched to the browser):
      ping     → {"result": "pong"}
      shutdown → gracefully terminates the process

    All other commands are resolved via getattr(browser, cmd) and called with
    the remaining request fields as keyword arguments.  Dataclass return values
    are automatically serialised to dicts.
    """

    def __init__(
        self,
        browser: BaseBraveBrowser,
        socket_path: Path,
        pid_path: Path,
    ) -> None:
        self._browser = browser
        self._socket_path = socket_path
        self._pid_path = pid_path

    async def run(self) -> None:
        """Start the daemon: bind socket, launch browser, serve until SIGTERM."""
        if is_daemon_running(self._pid_path):
            print(f"[daemon] already running", flush=True)
            return

        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._pid_path.write_text(str(os.getpid()))
        self._socket_path.unlink(missing_ok=True)

        ready = asyncio.Event()
        active_handlers: set[asyncio.Task] = set()

        async def start_browser() -> None:
            try:
                await self._browser.start()
                print("[daemon] browser ready", flush=True)
            except Exception as e:
                print(f"[daemon] browser error: {e}", flush=True)
            finally:
                ready.set()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            active_handlers.add(task)
            await ready.wait()
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        req = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    resp = await self._dispatch(req)
                    writer.write(json.dumps(resp).encode() + b"\n")
                    await writer.drain()
            except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError,
                    asyncio.CancelledError):
                pass
            finally:
                active_handlers.discard(task)
                writer.close()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        loop.add_signal_handler(signal.SIGINT, stop.set)

        # Bind socket before starting browser so clients can queue up immediately.
        server = await asyncio.start_unix_server(handle, path=str(self._socket_path))
        asyncio.create_task(start_browser())
        print(f"[daemon] listening on {self._socket_path}", flush=True)

        async with server:
            await stop.wait()

        # Cancel all open client connections so they don't hang after shutdown.
        for task in list(active_handlers):
            task.cancel()
        if active_handlers:
            await asyncio.gather(*active_handlers, return_exceptions=True)

        print("[daemon] shutting down…", flush=True)
        await self._browser.close()
        for p in (self._socket_path, self._pid_path):
            p.unlink(missing_ok=True)

    async def _dispatch(self, req: dict) -> dict:
        cmd = req.get("cmd", "")

        # Built-in commands.
        if cmd == "ping":
            return {"result": "pong"}
        if cmd == "shutdown":
            asyncio.get_running_loop().call_soon(
                lambda: os.kill(os.getpid(), signal.SIGTERM)
            )
            return {"result": None}

        # Block private/dunder access.
        if not cmd or cmd.startswith("_"):
            return {"error": f"unknown command: {cmd!r}"}

        method = getattr(self._browser, cmd, None)
        if method is None or not callable(method):
            return {"error": f"unknown command: {cmd!r}"}

        params = {k: v for k, v in req.items() if k != "cmd"}
        try:
            if asyncio.iscoroutinefunction(method):
                result = await method(**params)
            else:
                result = method(**params)

            # Automatically serialise dataclass return values.
            if dataclasses.is_dataclass(result) and not isinstance(result, type):
                result = dataclasses.asdict(result)
            elif (
                isinstance(result, list)
                and result
                and dataclasses.is_dataclass(result[0])
                and not isinstance(result[0], type)
            ):
                result = [dataclasses.asdict(x) for x in result]

            return {"result": result}
        except Exception as e:
            return {"error": str(e)}
