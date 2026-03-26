"""
Generic IPC client for Brave-backed TUI apps.

Any attribute access that is not defined on this class is turned into a
daemon command via __getattr__, so client.any_command(param=value) just
works without boilerplate.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path


class BraveClient:
    """
    Async IPC client that talks to a Daemon over a Unix socket.

    Usage::

        client = BraveClient(socket_path=Path("~/.config/myapp/daemon.sock"))
        await client.start()          # connect (retries while daemon boots)
        result = await client.some_browser_method(arg=value)
        await client.close()

    Any method call that is not explicitly defined here is forwarded to the
    daemon as {"cmd": "<method_name>", **kwargs}.  The daemon returns
    {"result": ...} which this client unwraps and returns.

    Subclasses can override specific methods to reconstruct typed return values
    (e.g. dataclasses) from the plain dicts that travel over the wire.
    """

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Connect to the daemon, retrying while it starts up (up to 15 s)."""
        for _ in range(30):
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(
                    str(self._socket_path)
                )
                return
            except (FileNotFoundError, ConnectionRefusedError):
                await asyncio.sleep(0.5)
        raise RuntimeError(f"Cannot connect to daemon at {self._socket_path}")

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------ IPC

    async def _call(self, cmd: str, **kwargs):
        """Send a command to the daemon and return the result."""
        async with self._lock:
            req = {"cmd": cmd, **kwargs}
            self._writer.write(json.dumps(req).encode() + b"\n")
            await self._writer.drain()
            line = await self._reader.readline()
            resp = json.loads(line)
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp.get("result")

    # ------------------------------------------------------------------ dynamic proxy

    def __getattr__(self, name: str):
        """
        Return a coroutine function that forwards any call to the daemon.

        This fires only for names not found on the instance/class, so
        start, close, and _call are never intercepted.
        """
        async def _proxy(**kwargs):
            return await self._call(name, **kwargs)
        _proxy.__name__ = name
        return _proxy
