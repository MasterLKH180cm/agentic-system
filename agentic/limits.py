"""Bound HTTP body intake before JSON parsing or multipart spooling."""
import asyncio
import json
import math

from starlette.types import ASGIApp, Receive, Scope, Send


class BodyLimitMiddleware:
    """Buffer a finite request body under one deadline, then replay it once."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int, timeout_seconds: float = 30):
        if not isinstance(max_body_bytes, int) or max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be a positive integer")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared_length = None
        limit = str(self.max_body_bytes).encode("ascii")
        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            value = value.strip()
            if not value or not value.isdigit():
                await self._reject(scope, send, 400, "Invalid Content-Length")
                return
            value = value.lstrip(b"0") or b"0"
            # Compare decimal lengths before int conversion, including hostile long headers.
            if (len(value), value) > (len(limit), limit):
                await self._reject(scope, send, 413, "Request body exceeds limit")
                return
            length = int(value)
            if declared_length is not None and length != declared_length:
                await self._reject(scope, send, 400, "Conflicting Content-Length")
                return
            declared_length = length

        body = bytearray()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._reject(scope, send, 408, "Request body receive timeout")
                return
            try:
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except TimeoutError:
                await self._reject(scope, send, 408, "Request body receive timeout")
                return
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                await self._reject(scope, send, 400, "Invalid request body")
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body_bytes:
                await self._reject(scope, send, 413, "Request body exceeds limit")
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        if declared_length is not None and len(body) != declared_length:
            await self._reject(scope, send, 400, "Content-Length does not match body")
            return
        buffered = bytes(body)
        del body
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": buffered, "more_body": False}
            # Preserve the server's real disconnect notification for streaming responses.
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope: Scope, send: Send, status: int, detail: str) -> None:
        payload = json.dumps({"detail": detail}).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]
        if scope.get("http_version", "1.1") in {"1.0", "1.1"}:
            headers.append((b"connection", b"close"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})
