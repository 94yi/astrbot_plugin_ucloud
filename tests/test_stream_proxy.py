import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_ucloud.stream_proxy import DownloadProxy


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = b"%PDF-example-content-EOF"
        self.account = {"username": "student"}
        self.plugin = SimpleNamespace(
            _store_lock=asyncio.Lock(), _read_accounts=AsyncMock(return_value={"session": self.account}),
            _userinfo=AsyncMock(return_value={}), _resource_refs=AsyncMock(return_value=["1"]),
            _client=SimpleNamespace(
                get_resource_metadata=AsyncMock(return_value=[{"id": "1", "name": "课件", "ext": "pdf", "fileSize": len(self.data)}]),
                get_resource_url=AsyncMock(return_value="https://fileucloud.bupt.edu.cn/test.pdf")))
        self.proxy = DownloadProxy(self.plugin)
        self.token = self.proxy.issue("https://bot.example", "session", "student", "course:2",
            self.plugin._client.get_resource_metadata.return_value[0]).rsplit("/", 1)[1]
        self.client = SimpleNamespace(get=AsyncMock(side_effect=self.get), close=AsyncMock())
        # SimpleNamespace is not hashable; production tracks active clients.
        self.client = type("Client", (), {"get": self.client.get, "close": self.client.close})()
        self.factory = patch("astrbot_plugin_ucloud.stream_proxy.aiohttp.ClientSession", return_value=self.client)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.upstream = None

    async def get(self, url, headers, **kwargs):
        start, end = 0, len(self.data) - 1
        status, response_headers = 200, {}
        if "Range" in headers:
            start, end = map(int, headers["Range"][6:].split("-"))
            status = 206
            response_headers["Content-Range"] = f"bytes {start}-{end}/{len(self.data)}"
        payload = self.data[start:end+1]
        async def chunks(size):
            for i in range(0, len(payload), 3):
                yield payload[i:i+3]
        self.upstream = SimpleNamespace(status=status, headers=response_headers, content_length=len(payload),
            content=SimpleNamespace(iter_chunked=chunks), close=lambda: None)
        return self.upstream

    async def test_streams_exact_bytes_and_closes_without_files(self):
        response = await self.proxy.response(self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join([x async for x in response.body_iterator]), self.data)
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertIn(".pdf", response.headers["content-disposition"])
        self.client.close.assert_awaited_once()
        self.assertEqual(self.proxy.slots._value, 2)

    async def test_range_and_suffix_resume(self):
        for header, data in [("bytes=3-7", self.data[3:8]), ("bytes=-4", self.data[-4:]), ("bytes=5-", self.data[5:])]:
            response = await self.proxy.response(self.token, header)
            self.assertEqual(response.status_code, 206)
            self.assertEqual(b"".join([x async for x in response.body_iterator]), data)

    async def test_invalid_expired_or_revoked_ticket(self):
        self.assertEqual((await self.proxy.response("invalid")).status_code, 410)
        self.plugin._read_accounts.return_value = {}
        self.assertEqual((await self.proxy.response(self.token)).status_code, 403)
        self.plugin._read_accounts.return_value = {"session": self.account}
        self.plugin._resource_refs.return_value = []
        self.assertEqual((await self.proxy.response(self.token)).status_code, 403)
        self.proxy.tickets[self.token]["expires"] = 0
        self.assertEqual((await self.proxy.response(self.token)).status_code, 410)
        self.client.get.assert_not_awaited()

    async def test_invalid_or_out_of_bounds_ranges(self):
        for header in ("bytes=1-2,4-5", "bytes=99-", "bytes=-0", "bytes=9-1"):
            self.assertEqual((await self.proxy.response(self.token, header)).status_code, 416)
        self.client.get.assert_not_awaited()

    async def test_untrusted_upstream_is_rejected(self):
        self.plugin._client.get_resource_url.return_value = "https://evil.example/file"
        self.assertEqual((await self.proxy.response(self.token)).status_code, 502)
        self.client.get.assert_not_awaited()
        self.assertEqual(self.proxy.slots._value, 2)

    async def test_changed_version_prevents_mixed_resume(self):
        self.plugin._client.get_resource_metadata.return_value[0]["updateTime"] = "new-version"
        self.assertEqual((await self.proxy.response(self.token, "bytes=3-")).status_code, 409)
        self.client.get.assert_not_awaited()

    async def test_disconnect_closes_upstream_and_releases_slot(self):
        response = await self.proxy.response(self.token)
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        self.client.close.assert_awaited_once()
        self.assertEqual(self.proxy.slots._value, 2)

    async def test_wrong_upstream_range_is_rejected(self):
        async def wrong(*args, **kwargs):
            upstream = await self.get(*args, **kwargs)
            upstream.headers["Content-Range"] = "bytes 0-2/999"
            return upstream
        self.client.get.side_effect = wrong
        self.assertEqual((await self.proxy.response(self.token, "bytes=1-2")).status_code, 502)
        self.client.close.assert_awaited_once()

    def test_invalid_base_and_ticket_bounds(self):
        with self.assertRaises(ValueError):
            self.proxy.issue("", "session", "student", "course:2", {"id": "1"})
        self.proxy.tickets = {str(i): {"expires": 1000} for i in range(256)}
        with self.assertRaises(ValueError):
            self.proxy.issue("https://bot.example", "session", "student", "course:2", {"id": "1"}, now=1)
