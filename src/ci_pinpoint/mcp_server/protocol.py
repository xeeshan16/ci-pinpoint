"""A small Model Context Protocol server over stdio (JSON-RPC 2.0, one message per line).

Only what a tools-only server needs: the ``initialize`` handshake (protocol
versions 2024-11-05 to 2025-11-25), ``ping``, ``tools/list``, ``tools/call``,
progress notifications and cancellation. Clients that first probe the newer
``server/discover`` get "method not found" and fall back to the handshake, as the
specification asks of them.

Tool calls run on worker threads, so a slow model call does not block ``ping``
or a quick ``search_log``. Responses are written under a lock, one line each.
Nothing but protocol messages is ever written to stdout; logs go to stderr.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TextIO

log = logging.getLogger(__name__)

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
STRUCTURED_SINCE = "2025-06-18"  # outputSchema and structuredContent
TITLES_SINCE = "2025-06-18"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_CONCURRENT_CALLS = 4


class InvalidParamsError(ValueError):
    """Bad tool arguments: reported to the client as a JSON-RPC error."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    text: str
    structured: Mapping[str, Any] | None = None
    is_error: bool = False


Progress = Callable[[float, float | None, str], None]
Handler = Callable[[Mapping[str, Any], Progress], ToolResult]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Handler
    output_schema: Mapping[str, Any] | None = None
    annotations: Mapping[str, Any] = field(default_factory=dict)

    def describe(self, version: str) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": dict(self.input_schema),
        }
        if version >= TITLES_SINCE:
            spec["title"] = self.title
            if self.annotations:
                spec["annotations"] = {"title": self.title, **self.annotations}
        elif self.annotations and version >= "2025-03-26":
            spec["annotations"] = dict(self.annotations)
        if self.output_schema is not None and version >= STRUCTURED_SINCE:
            spec["outputSchema"] = dict(self.output_schema)
        return spec


def _error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _result(id_: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": dict(result)}


class StdioServer:
    def __init__(
        self,
        tools: list[Tool],
        *,
        name: str,
        title: str,
        version: str,
        instructions: str | None = None,
        max_concurrent: int = MAX_CONCURRENT_CALLS,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._info = {"name": name, "title": title, "version": version}
        self._instructions = instructions
        self._version = PROTOCOL_VERSIONS[0]
        self._out: TextIO | None = None
        self._write_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._cancelled: set[Any] = set()
        self._inflight: dict[Any, threading.Thread] = {}
        self._state_lock = threading.Lock()

    # transport -------------------------------------------------------------

    def _send(self, message: Mapping[str, Any]) -> None:
        if self._out is None:  # pragma: no cover - serve() sets it first
            return
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            self._out.write(line + "\n")
            self._out.flush()

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        """Read messages until stdin closes, then wait briefly for running calls."""
        self._out = stdout
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            if len(line) > MAX_MESSAGE_BYTES:
                self._send(_error(None, INVALID_REQUEST, "message too large"))
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                self._send(_error(None, PARSE_ERROR, f"parse error: {exc.msg}"))
                continue
            if isinstance(message, list):  # batches (2025-03-26 only)
                replies = [r for r in (self.handle(m) for m in message) if r is not None]
                if replies:
                    with self._write_lock:
                        self._out.write(json.dumps(replies, ensure_ascii=False) + "\n")
                        self._out.flush()
                continue
            reply = self.handle(message)
            if reply is not None:
                self._send(reply)
        self._drain(timeout_s=5.0)

    def _drain(self, timeout_s: float) -> None:
        with self._state_lock:
            threads = list(self._inflight.values())
        for thread in threads:
            thread.join(timeout_s)

    # dispatch --------------------------------------------------------------

    def handle(self, message: Any) -> dict[str, Any] | None:
        """Handle one message; returns the reply, or None (notification, or async call)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        id_ = message.get("id")
        is_request = "id" in message
        if not isinstance(method, str):
            if "result" in message or "error" in message:
                return None  # a response; this server sends no requests, so nothing waits
            return _error(id_, INVALID_REQUEST, "missing method")
        params = message.get("params") or {}
        if not is_request:
            if isinstance(params, dict):
                self._notification(method, params)
            return None
        try:
            if not isinstance(params, dict):
                raise InvalidParamsError("params must be an object")
            return self._request(id_, method, params)
        except InvalidParamsError as exc:
            return _error(id_, INVALID_PARAMS, str(exc))

    def _request(self, id_: Any, method: str, params: Mapping[str, Any]) -> dict[str, Any] | None:
        if method == "tools/call":
            return self._call(id_, params)
        if method == "initialize":
            result = self._initialize(params)
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [t.describe(self._version) for t in self._tools.values()]}
        else:
            return _error(id_, METHOD_NOT_FOUND, f"method not found: {method}")
        return _result(id_, result)

    def _notification(self, method: str, params: Mapping[str, Any]) -> None:
        if method == "notifications/cancelled":
            with self._state_lock:
                if params.get("requestId") in self._inflight:
                    self._cancelled.add(params.get("requestId"))
            log.info("request cancelled", extra={"request_id": params.get("requestId")})

    def _initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        self._version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        client = params.get("clientInfo") or {}
        log.info(
            "client connected",
            extra={"client": client.get("name"), "protocol": self._version},
        )
        info = (
            dict(self._info)
            if self._version >= TITLES_SINCE
            else {k: v for k, v in self._info.items() if k != "title"}
        )
        result: dict[str, Any] = {
            "protocolVersion": self._version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": info,
        }
        if self._instructions:
            result["instructions"] = self._instructions
        return result

    def _call(self, id_: Any, params: Mapping[str, Any]) -> dict[str, Any] | None:
        name = params.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise InvalidParamsError(f"unknown tool: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise InvalidParamsError("arguments must be an object")
        meta = params.get("_meta") or {}
        token = meta.get("progressToken") if isinstance(meta, dict) else None
        if self._out is None:
            # Called directly (tests): run inline and return the reply.
            return self._run(id_, tool, arguments, token, reply_inline=True)
        thread = threading.Thread(
            target=self._run,
            args=(id_, tool, arguments, token),
            name=f"tool-{tool.name}",
            daemon=True,
        )
        with self._state_lock:
            self._inflight[id_] = thread
        thread.start()
        return None

    def _run(
        self,
        id_: Any,
        tool: Tool,
        arguments: Mapping[str, Any],
        token: Any,
        *,
        reply_inline: bool = False,
    ) -> dict[str, Any] | None:
        def progress(done: float, total: float | None, message: str) -> None:
            if token is None or self._out is None:
                return
            payload: dict[str, Any] = {"progressToken": token, "progress": done}
            if total is not None:
                payload["total"] = total
            if self._version >= "2025-03-26":
                payload["message"] = message
            self._send({"jsonrpc": "2.0", "method": "notifications/progress", "params": payload})

        with self._slots:
            try:
                result = tool.handler(arguments, progress)
                reply = _result(id_, self._tool_result(tool, result))
            except InvalidParamsError as exc:
                reply = _error(id_, INVALID_PARAMS, str(exc))
            except Exception as exc:
                log.exception("tool failed", extra={"tool": tool.name})
                reply = _result(
                    id_,
                    {
                        "content": [{"type": "text", "text": f"{tool.name} failed: {exc}"}],
                        "isError": True,
                    },
                )
        with self._state_lock:
            self._inflight.pop(id_, None)
            cancelled = id_ in self._cancelled
            self._cancelled.discard(id_)
        if reply_inline:
            return reply
        if not cancelled:
            self._send(reply)
        return None

    def _tool_result(self, tool: Tool, result: ToolResult) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "content": [{"type": "text", "text": result.text}],
            "isError": result.is_error,
        }
        if (
            result.structured is not None
            and tool.output_schema is not None
            and self._version >= STRUCTURED_SINCE
            and not result.is_error
        ):
            payload["structuredContent"] = dict(result.structured)
        return payload
