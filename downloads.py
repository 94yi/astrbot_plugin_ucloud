"""Private, integrity-checked Teaching Cloud cache. No credentials/URLs in SQLite."""

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import tempfile
import zipfile
from pathlib import Path

import aiohttp
from yarl import URL

from .ucloud_client import UCloudAPIError

HOSTS = {"fileucloud.bupt.edu.cn", "apiucloud.bupt.edu.cn", "ucloud.bupt.edu.cn"}
# The authenticated UCloud filePath API returns this official campus origin;
# deployment DNS resolves it to this campus-only address. Never allow arbitrary
# RFC1918 addresses or apply this exception to another hostname.
CAMPUS_ADDRESSES = {"fileucloud.bupt.edu.cn": {"10.3.19.2"}}


def checked_url(value):
    try:
        url = URL(value)
        if (url.scheme != "https" or url.host not in HOSTS or url.port != 443
                or url.user or url.password or "\\" in value
                or any(ord(c) < 33 for c in value)):
            raise ValueError()
        return url
    except (ValueError, TypeError):
        raise UCloudAPIError("附件下载地址不在已验证的教学云 HTTPS 域名范围内") from None


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.delegate = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host not in HOSTS:
            raise UCloudAPIError("附件下载域名不受信任")
        records = await self.delegate.resolve(host, port, family)
        if not records:
            raise UCloudAPIError("附件下载域名无法解析")
        for item in records:
            address = ipaddress.ip_address(item["host"])
            campus = item["host"] in CAMPUS_ADDRESSES.get(host, set())
            if (not address.is_global and not campus) or address.is_multicast or "%" in item["host"]:
                raise UCloudAPIError("禁止下载内网地址")
        return records

    async def close(self):
        await self.delegate.close()


async def disk_call(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def digest_file(path, algorithm="sha256"):
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def resource_version(metadata):
    fields = {key: metadata.get(key) for key in
              ("storageId", "updateTime", "fileSize", "ext", "md5", "sha256")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def validate_file(path, metadata):
    size = path.stat().st_size
    expected = metadata.get("fileSize")
    if expected is not None and (isinstance(expected, bool) or not str(expected).isdigit()):
        raise UCloudAPIError("教学云附件字节大小字段无效")
    if expected is not None and size != int(expected):
        raise UCloudAPIError("附件大小与教学云清单不一致，未保存为有效文件")
    if not size:
        raise UCloudAPIError("附件为空，未标记下载成功")
    with path.open("rb") as stream:
        header = stream.read(512).lstrip()
        stream.seek(max(0, size - 2048))
        tail = stream.read()
    ext = str(metadata.get("ext") or Path(str(metadata.get("name", ""))).suffix.lstrip(".")).lower()
    if ext not in {"html", "htm", "txt", "json", "xml"} and header.lower().startswith((b"<!doctype html", b"<html", b'{"code"', b'{"success"')):
        raise UCloudAPIError("下载结果是登录页或错误响应，不是附件")
    if ext == "pdf" and (not header.startswith(b"%PDF-") or b"%%EOF" not in tail):
        raise UCloudAPIError("PDF 文件头或结束标记无效")
    if ext in {"doc", "xls", "ppt"} and not header.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        raise UCloudAPIError("Office 文件头无效")
    if ext in {"zip", "docx", "xlsx", "pptx", "odt", "ods", "odp"}:
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(i.file_size for i in entries) > 512 * 1024**2:
                    raise UCloudAPIError("压缩文件解压校验超过安全上限")
                if ext in {"docx", "xlsx", "pptx"} and "[Content_Types].xml" not in archive.namelist():
                    raise UCloudAPIError("Office 压缩包缺少格式描述")
                if archive.testzip() is not None:
                    raise UCloudAPIError("压缩文件 CRC 校验失败")
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
            raise UCloudAPIError("压缩文件损坏或无法校验（含加密压缩包）") from None
    sha = digest_file(path)
    for key, length in (("sha256", 64), ("md5", 32)):
        expected_hash = metadata.get(key)
        if expected_hash:
            if not re.fullmatch(rf"[a-fA-F0-9]{{{length}}}", str(expected_hash)):
                raise UCloudAPIError("教学云校验和格式无效")
            actual = sha if key == "sha256" else digest_file(path, "md5")
            if actual != str(expected_hash).lower():
                raise UCloudAPIError("附件校验和与服务端不一致")
    return size, sha


class DownloadStore:
    def __init__(self, root, max_bytes=200 * 1024**2, max_storage=2 * 1024**3, min_free=512 * 1024**2):
        self.root = Path(root)
        self.max_bytes, self.max_storage, self.min_free = max_bytes, max_storage, min_free
        self.lock = asyncio.Lock()  # One writer/download, including concurrent repeated commands.

    def database(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise UCloudAPIError("下载目录不安全")
        path = self.root / "downloads.sqlite3"
        if path.is_symlink():
            raise UCloudAPIError("下载索引路径不安全")
        db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS resources (scope TEXT, resource TEXT, version TEXT, sha TEXT, size INTEGER, path TEXT, PRIMARY KEY(scope,resource))")
            db.execute("CREATE INDEX IF NOT EXISTS resources_content ON resources(scope,sha)")
            db.commit()
        except sqlite3.DatabaseError:
            db.close()
            raise UCloudAPIError("下载索引无法打开，请检查 SQLite 数据库；未删除或重建已有索引") from None
        return db

    def valid_cached(self, row):
        if not row:
            return None
        sha, size, value = row
        path = self.root / value
        if (path.is_symlink() or any(p.is_symlink() for p in path.parents)
                or not path.resolve().is_relative_to(self.root.resolve()) or not path.is_file()
                or path.stat().st_size != size or digest_file(path) != sha):
            return None
        return path

    def lookup(self, scope, resource, version):
        db = self.database()
        try:
            return self.valid_cached(db.execute("SELECT sha,size,path FROM resources WHERE scope=? AND resource=? AND version=?", (scope, resource, version)).fetchone())
        except sqlite3.DatabaseError:
            raise UCloudAPIError("下载索引读取失败，未跳过下载；请检查 SQLite 数据库") from None
        finally:
            db.close()

    def commit(self, scope, resource, version, partial, size, sha, name):
        db = self.database()
        try:
            row = db.execute("SELECT sha,size,path FROM resources WHERE scope=? AND sha=? LIMIT 1", (scope, sha)).fetchone()
            target = self.valid_cached(row)
            duplicate = target is not None
            if target is None:
                target = partial.parent / name
                os.replace(partial, target)
            db.execute("INSERT OR REPLACE INTO resources VALUES (?,?,?,?,?,?)",
                       (scope, resource, version, sha, size, str(target.relative_to(self.root))))
            db.commit()
            return target, duplicate
        except sqlite3.DatabaseError:
            raise UCloudAPIError("下载索引保存失败，未报告成功，文件副本保留") from None
        finally:
            db.close()

    async def remove_delivered(self, path, sha):
        """Remove only a verified, indexed cache file after confirmed delivery."""
        async with self.lock:
            return await disk_call(self._remove_delivered, Path(path), sha)

    def _remove_delivered(self, path, sha):
        root = self.root.resolve()
        if (path.is_symlink() or any(p.is_symlink() for p in path.parents)
                or not path.resolve().is_relative_to(root) or path.resolve() == root):
            raise UCloudAPIError("清理路径不安全，文件保留")
        relative = str(path.resolve().relative_to(root))
        db = self.database()
        try:
            rows = db.execute("SELECT sha FROM resources WHERE path=?", (relative,)).fetchall()
            if not rows:
                if not path.exists():
                    return False
                raise UCloudAPIError("文件不在下载索引中，未删除")
            if any(row[0] != sha for row in rows):
                raise UCloudAPIError("下载索引内容不一致，未删除")
            if path.exists():
                if not path.is_file() or digest_file(path) != sha:
                    raise UCloudAPIError("文件内容已变化，未删除")
                path.unlink()
            db.execute("DELETE FROM resources WHERE path=?", (relative,))
            db.commit()
            try:
                path.parent.rmdir()
            except OSError:
                pass
            return True
        finally:
            db.close()

    def capacity(self):
        used = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file() and not p.is_symlink())
        if used + self.max_bytes > self.max_storage or shutil.disk_usage(self.root).free < self.min_free + self.max_bytes:
            raise UCloudAPIError("下载存储额度或磁盘空间不足；未自动删除已有文件")

    async def transfer(self, url, output):
        resolver = PublicResolver()
        connector = aiohttp.TCPConnector(resolver=resolver)
        try:
            async with aiohttp.ClientSession(connector=connector, trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
                    timeout=aiohttp.ClientTimeout(total=600, connect=30, sock_read=60)) as client:
                for hop in range(6):
                    url = checked_url(str(url))
                    # Signed file URL only: no CAS cookie, blade-auth or bearer token.
                    async with client.get(url, allow_redirects=False, headers={"Accept-Encoding": "identity"}) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if hop == 5 or not location:
                                raise UCloudAPIError("附件重定向超过上限或缺少地址")
                            if "\\" in location or any(ord(c) < 33 for c in location):
                                raise UCloudAPIError("附件重定向地址无效")
                            url = checked_url(str(url.join(URL(location))))
                            continue
                        if response.status != 200:
                            raise UCloudAPIError(f"附件下载 HTTP {response.status}，未标记成功")
                        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                            raise UCloudAPIError("附件传输编码不受支持")
                        length = response.content_length
                        if length is not None and length > self.max_bytes:
                            raise UCloudAPIError("附件超过单文件下载上限")
                        count = 0
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            count += len(chunk)
                            if count > self.max_bytes:
                                raise UCloudAPIError("附件超过单文件下载上限")
                            await disk_call(output.write, chunk)
                        if length is not None and count != length:
                            raise UCloudAPIError("附件传输不完整")
                        return
        finally:
            await connector.close()
            await resolver.close()

    async def download(self, scope, metadata, get_url):
        scope = hashlib.sha256(scope.encode()).hexdigest()
        resource = str(metadata.get("id") or "")
        if not resource:
            raise UCloudAPIError("附件缺少资源标识")
        size = metadata.get("fileSize")
        if size is not None and (not str(size).isdigit() or int(size) > self.max_bytes):
            raise UCloudAPIError("附件大小无效或超过单文件上限")
        version = resource_version(metadata)
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", str(metadata.get("name") or "附件")).strip(" .")[:70] or "附件"
        name = name.replace("..", "_")
        ext = str(metadata.get("ext") or "").lower()
        if re.fullmatch(r"[a-z0-9]{1,10}", ext) and not name.lower().endswith("." + ext):
            name += "." + ext
        async with self.lock:
            # No reliable revision? Re-fetch and deduplicate content, rather
            # than silently serving a potentially stale same-ID resource.
            cached = await disk_call(self.lookup, scope, resource, version)
            if cached and (metadata.get("updateTime") or metadata.get("sha256") or metadata.get("md5")):
                return {"path": cached, "reused": True, "sha256": await disk_call(digest_file, cached)}
            await disk_call(self.capacity)
            folder = self.root / scope
            if folder.is_symlink():
                raise UCloudAPIError("会话下载目录不安全")
            folder.mkdir(mode=0o700, exist_ok=True)
            task_dir = Path(tempfile.mkdtemp(prefix="file-", dir=folder))
            partial = task_dir / ".download.part"
            try:
                url = checked_url(await get_url())
                with os.fdopen(os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as output:
                    await self.transfer(url, output)
                    await disk_call(output.flush)
                    await disk_call(os.fsync, output.fileno())
                size, sha = await disk_call(validate_file, partial, metadata)
                target, duplicate = await disk_call(self.commit, scope, resource, version, partial, size, sha, name)
                return {"path": target, "reused": duplicate, "sha256": sha}
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise UCloudAPIError("附件下载连接失败或超时；未标记成功，可重新下载") from None
            finally:
                partial.unlink(missing_ok=True)
                try:
                    task_dir.rmdir()
                except OSError:
                    pass
