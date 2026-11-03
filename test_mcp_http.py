import asyncio
import unittest

from mcp_http import BearerAuthMiddleware, _csv


class DummyApp:
    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def request(app, authorization=None, duplicate=False):
    headers = [(b"host", b"kicad-mcp.nlab.ltd")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
        if duplicate:
            headers.append((b"authorization", authorization.encode()))
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app({"type": "http", "method": "POST", "path": "/mcp", "headers": headers}, receive, send)
    return messages[0]["status"]


class BearerAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_exact_single_bearer_reaches_mcp(self):
        inner = DummyApp()
        app = BearerAuthMiddleware(inner, "correct-token")
        self.assertEqual(await request(app), 401)
        self.assertEqual(await request(app, "Basic correct-token"), 401)
        self.assertEqual(await request(app, "Bearer wrong-token"), 401)
        self.assertEqual(await request(app, "Bearer correct-token", duplicate=True), 401)
        self.assertEqual(await request(app, "bEaReR correct-token"), 204)
        self.assertEqual(inner.calls, 1)

    async def test_non_http_scope_is_not_intercepted(self):
        inner = DummyApp()
        app = BearerAuthMiddleware(inner, "correct-token")
        messages = []

        async def receive():
            return {"type": "websocket.disconnect"}

        async def send(message):
            messages.append(message)

        await app({"type": "websocket", "path": "/mcp", "headers": []}, receive, send)
        self.assertEqual(inner.calls, 1)

    def test_allowlist_parser_rejects_empty_override(self):
        self.assertEqual(_csv(None, ("default",)), ["default"])
        self.assertEqual(_csv("one, two", ()), ["one", "two"])
        with self.assertRaises(ValueError):
            _csv(" , ", ("default",))


if __name__ == "__main__":
    unittest.main()
