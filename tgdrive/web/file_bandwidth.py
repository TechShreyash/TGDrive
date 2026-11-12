"""Pace explicit file routes without buffering a response or changing its owner."""

from __future__ import annotations

import asyncio

from tgdrive.transfers.bandwidth import PayloadBudget

FILE_ROUTES = frozenset(
    {
        "/file",
        "/api/v1/books",
        "/api/v1/thumbnail",
        "/api/v1/poster",
        "/api/v1/replica/file",
    }
)


async def paced_body(body: bytes, budget: PayloadBudget | None):
    offset = 0
    while offset < len(body):
        size = min(
            len(body) - offset, budget.quantum() if budget is not None else len(body)
        )
        block = body[offset : offset + size]
        if budget is not None:
            await budget.acquire(len(block))
        offset += size
        yield block


class PacedReceive:
    def __init__(self, receive, budget: PayloadBudget):
        self.receive, self.budget = receive, budget
        self.pending = None
        self.offset = 0

    async def __call__(self):
        if self.pending is None:
            self.pending = await self.receive()
            self.offset = 0
        message = self.pending
        body = message.get("body", b"")
        if message["type"] != "http.request" or not body:
            self.pending = None
            return message
        stop = min(len(body), self.offset + self.budget.quantum())
        block = body[self.offset : stop]
        await self.budget.acquire(len(block))
        self.offset = stop
        result = {
            **message,
            "body": block,
            "more_body": stop < len(body) or message.get("more_body", False),
        }
        if stop == len(body):
            self.pending = None
        return result


class FileBandwidthMiddleware:
    def __init__(self, app, *, bandwidth):
        self.app, self.bandwidth = app, bandwidth

    async def __call__(self, scope, receive, send):
        if (
            self.bandwidth is None
            or scope["type"] != "http"
            or scope.get("method") != "GET"
            or scope.get("path") not in FILE_ROUTES
        ):
            return await self.app(scope, receive, send)
        scope = {
            **scope,
            "extensions": {
                k: v
                for k, v in scope.get("extensions", {}).items()
                if k != "http.response.pathsend"
            },
        }
        messages = []
        condition = asyncio.Condition()
        disconnected = asyncio.Event()
        success = False

        async def relay():
            # Only this task reads upstream; StreamingResponse receives the relay.
            while True:
                message = await receive()
                async with condition:
                    if message["type"] == "http.disconnect":
                        disconnected.set()
                        condition.notify_all()
                        return
                    while messages:
                        await condition.wait()
                    messages.append(message)
                    condition.notify_all()

        async def read():
            async with condition:
                while not messages and not disconnected.is_set():
                    await condition.wait()
                if messages:
                    message = messages.pop()
                    condition.notify_all()
                    return message
                return {"type": "http.disconnect"}

        async def write(message):
            nonlocal success
            if message["type"] == "http.response.start":
                success = message["status"] in {200, 206}
            if (
                success
                and message["type"] == "http.response.body"
                and message.get("body")
            ):
                body = message["body"]
                offset = 0
                async for block in paced_body(body, self.bandwidth.upload):
                    offset += len(block)
                    await send(
                        {
                            **message,
                            "body": block,
                            "more_body": offset < len(body)
                            or message.get("more_body", False),
                        }
                    )
            else:
                await send(message)

        pump = asyncio.create_task(relay())
        closed = asyncio.create_task(disconnected.wait())
        application = asyncio.create_task(self.app(scope, read, write))
        try:
            await asyncio.wait(
                (application, closed, pump), return_when=asyncio.FIRST_COMPLETED
            )
            if pump.done() and not disconnected.is_set():
                await pump
            if disconnected.is_set() and not application.done():
                application.cancel()
            await application
        finally:
            for task in (pump, closed, application):
                task.cancel()
            await asyncio.gather(pump, closed, application, return_exceptions=True)
