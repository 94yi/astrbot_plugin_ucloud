"""Expiring, permission-rechecked downloads with bounded memory and no disk IO."""

import asyncio
import re
import secrets
import time
from urllib.parse import quote, urlsplit

import aiohttp
from starlette.background import BackgroundTask
from starlette.responses import PlainTextResponse, StreamingResponse
from yarl import URL

from .downloads import PublicResolver, checked_url, resource_version


class DownloadProxy:
    def __init__(self, plugin):
        self.plugin = plugin
        self.tickets = {}
        self.slots = asyncio.Semaphore(2)
        self.clients = set()

    def issue(self, base, origin, username, reference, item, now=None):
        parsed = urlsplit(base)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("请先配置电脑可访问的 proxy_public_base_url（AstrBot管理页地址）。")
        now = time.time() if now is None else now
        self.tickets = {k: v for k, v in self.tickets.items() if v["expires"] > now}
        if len(self.tickets) >= 256:
            raise ValueError("临时下载链接数量已达上限，请稍后再试。")
        token = secrets.token_urlsafe(32)
        self.tickets[token] = {"origin": origin, "username": username,
                               "reference": reference, "id": str(item["id"]),
                               "version": resource_version(item),
                               "expires": now + 3600}
        return base.rstrip("/") + "/api/plug/astrbot_plugin_ucloud/download/" + token

    async def close(self):
        self.tickets.clear()
        await asyncio.gather(*(client.close() for client in list(self.clients)), return_exceptions=True)

    async def response(self, token, range_header=None):
        ticket = self.tickets.get(token)
        if not ticket or ticket["expires"] <= time.time():
            return PlainTextResponse("下载链接无效或已过期，请在原会话重新获取。", 410)
        if range_header and not re.fullmatch(r"bytes=(?:[0-9]+-[0-9]*|-[0-9]+)", range_header):
            return PlainTextResponse("仅支持单段字节范围。", 416)
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=0.1)
        except TimeoutError:
            return PlainTextResponse("下载并发已满，请稍后重试。", 429, headers={"Retry-After": "5"})
        client = upstream = None
        released = False

        async def cleanup():
            nonlocal released
            if released:
                return
            released = True
            try:
                if upstream is not None:
                    upstream.close()
                if client is not None:
                    await client.close()
                    self.clients.discard(client)
            finally:
                self.slots.release()

        try:
            async with self.plugin._store_lock:
                account = (await self.plugin._read_accounts()).get(ticket["origin"])
            if not account or account["username"] != ticket["username"]:
                await cleanup()
                return PlainTextResponse("账号已退出或切换，请重新获取链接。", 403)
            userinfo = await self.plugin._userinfo(account)
            allowed = await self.plugin._resource_refs(userinfo, ticket["reference"])
            if ticket["id"] not in allowed:
                await cleanup()
                return PlainTextResponse("附件已撤回或当前账号无权访问。", 403)
            metadata = await self.plugin._client.get_resource_metadata(userinfo, [ticket["id"]])
            item = metadata[0]
            if resource_version(item) != ticket["version"]:
                await cleanup()
                return PlainTextResponse("附件版本已变化，请重新获取链接并重新下载。", 409)
            size = int(item["fileSize"])
            if size < 0 or str(item["id"]) != ticket["id"]:
                raise ValueError("Invalid resource metadata")
            start, end = 0, size - 1
            if range_header:
                first, last = range_header[6:].split("-")
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    count = int(last)
                    start = max(0, size - count)
                if start > end or start >= size or (not first and count == 0):
                    await cleanup()
                    return PlainTextResponse("请求范围超出文件大小。", 416, headers={"Content-Range": f"bytes */{size}"})
            url = checked_url(await self.plugin._client.get_resource_url(userinfo, ticket["id"]))
            client = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=PublicResolver()),
                cookie_jar=aiohttp.DummyCookieJar(), trust_env=False, auto_decompress=False,
                timeout=aiohttp.ClientTimeout(total=3600, connect=30, sock_read=60),
            )
            self.clients.add(client)
            headers = {"Accept-Encoding": "identity"}
            if range_header:
                headers["Range"] = f"bytes={start}-{end}"
            for hop in range(6):
                upstream = await client.get(url, headers=headers, allow_redirects=False)
                if upstream.status not in {301, 302, 303, 307, 308}:
                    break
                location = upstream.headers.get("Location", "")
                upstream.close()
                if hop == 5 or not location or "\\" in location or any(ord(c) < 33 for c in location):
                    raise ValueError("Invalid upstream redirect")
                url = checked_url(str(url.join(URL(location))))
            expected_status = 206 if range_header else 200
            expected_length = end - start + 1
            if (upstream.status != expected_status
                    or upstream.headers.get("Content-Encoding", "identity") != "identity"
                    or (upstream.content_length is not None and upstream.content_length != expected_length)):
                raise ValueError("Invalid upstream response")
            if range_header and upstream.headers.get("Content-Range") != f"bytes {start}-{end}/{size}":
                raise ValueError("Invalid upstream range")
            etag = upstream.headers.get("ETag")
            if ticket.get("etag") and ticket["etag"] != etag:
                await cleanup()
                return PlainTextResponse("上游文件版本已变化，请重新下载。", 409)
            if etag:
                ticket["etag"] = etag
            name = str(item.get("name") or "attachment").replace("/", "_").replace("\\", "_")
            name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)[:180]
            ext = str(item.get("ext") or "")
            if re.fullmatch(r"[a-zA-Z0-9]{1,10}", ext) and not name.lower().endswith("." + ext.lower()):
                name += "." + ext
            out_headers = {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe=""),
                           "Content-Length": str(expected_length), "Accept-Ranges": "bytes",
                           "Cache-Control": "no-store, private", "X-Accel-Buffering": "no",
                           "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
            if range_header:
                out_headers["Content-Range"] = f"bytes {start}-{end}/{size}"

            async def body():
                count = 0
                try:
                    async for chunk in upstream.content.iter_chunked(65536):
                        count += len(chunk)
                        if count > expected_length:
                            raise OSError("Upstream exceeded expected length")
                        yield chunk
                    if count != expected_length:
                        raise OSError("Upstream ended before expected length")
                finally:
                    await cleanup()

            return StreamingResponse(body(), status_code=expected_status, headers=out_headers,
                                     media_type="application/octet-stream", background=BackgroundTask(cleanup))
        except asyncio.CancelledError:
            await cleanup()
            raise
        except Exception:
            await cleanup()
            return PlainTextResponse("教学云连接或文件校验失败，请重新获取链接后重试。", 502,
                                     headers={"Cache-Control": "no-store"})
