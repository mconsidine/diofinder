"""
Maintenance Unix socket protocol.

Wire format: one JSON object per line, both directions.

Request from client:
  {"cmd": "status", "args": {}}

Response from server (success):
  {"ok": true, "result": {...arbitrary JSON...}}

Response from server (failure):
  {"ok": false, "error": "human-readable explanation"}

Commands and their args/results are documented in the command
dispatch table in comms_proc._handle_maint_command.

The socket lives at /run/diofinder/maint.sock, owned by diofinder:diofinder
with mode 0660. Add yourself to the diofinder group to use diofinder-ctl
without sudo.
"""

import dataclasses
import json
import os
import socket
from typing import Any, Optional

SOCKET_PATH = os.environ.get("DIOFINDER_MAINT_SOCKET", "/run/diofinder/maint.sock")


@dataclasses.dataclass
class MaintRequest:
    cmd: str
    args: dict

    def encode(self) -> bytes:
        return (json.dumps({"cmd": self.cmd, "args": self.args}) + "\n").encode("utf-8")

    @classmethod
    def decode(cls, line: bytes) -> "MaintRequest":
        obj = json.loads(line.decode("utf-8"))
        return cls(cmd=str(obj.get("cmd", "")), args=dict(obj.get("args") or {}))


@dataclasses.dataclass
class MaintResponse:
    ok: bool
    result: Any = None
    error: str = ""

    def encode(self) -> bytes:
        if self.ok:
            return (json.dumps({"ok": True, "result": self.result}) + "\n").encode("utf-8")
        return (json.dumps({"ok": False, "error": self.error}) + "\n").encode("utf-8")

    @classmethod
    def decode(cls, line: bytes) -> "MaintResponse":
        obj = json.loads(line.decode("utf-8"))
        return cls(
            ok=bool(obj.get("ok", False)),
            result=obj.get("result"),
            error=str(obj.get("error", "")),
        )


def call(cmd: str, args: Optional[dict] = None,
         socket_path: Optional[str] = None,
         timeout: float = 10.0) -> MaintResponse:
    """Synchronous client helper: send one request, read one response.

    socket_path defaults to the module-level SOCKET_PATH (which respects
    the DIOFINDER_MAINT_SOCKET environment variable at import time).
    """
    if socket_path is None:
        socket_path = SOCKET_PATH
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(socket_path)
    try:
        s.sendall(MaintRequest(cmd=cmd, args=args or {}).encode())
        # Read one newline-terminated response
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        line, _, _ = buf.partition(b"\n")
        if not line:
            return MaintResponse(ok=False, error="empty response from server")
        return MaintResponse.decode(line)
    finally:
        try:
            s.close()
        except Exception:
            pass
