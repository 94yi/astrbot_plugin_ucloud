import asyncio
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

from astrbot_plugin_ucloud.downloads import (
    DownloadStore,
    PublicResolver,
    checked_url,
    validate_file,
)
from astrbot_plugin_ucloud.ucloud_client import DirectUCloudClient, UCloudAPIError


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "downloads"
        self.store = DownloadStore(self.root, max_bytes=4096, min_free=0)
        self.data = b"%PDF-1.7\nfixture\n%%EOF\n"
        self.metadata = {"id": "1", "name": "教案.pdf", "ext": "pdf", "fileSize": len(self.data), "updateTime": "v1", "storageId": "object1"}
        self.get_url = AsyncMock(return_value="https://fileucloud.bupt.edu.cn/file?secret=private")
        async def transfer(url, output):
            output.write(self.data)
        self.store.transfer = AsyncMock(side_effect=transfer)

    async def fetch(self, scope="account-one", **changes):
        return await self.store.download(scope, {**self.metadata, **changes}, self.get_url)

    async def test_delivered_cleanup_removes_exact_file_and_shared_index_only(self):
        first = await self.fetch()
        same_content = await self.fetch(id="2")
        other_scope = await self.fetch(scope="account-two")
        self.assertEqual(first["path"], same_content["path"])
        await self.store.remove_delivered(first["path"], first["sha256"])
        self.assertFalse(first["path"].exists())
        self.assertTrue(other_scope["path"].exists())
        with self.store.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM resources").fetchone()[0], 1)
        self.assertFalse(await self.store.remove_delivered(first["path"], first["sha256"]))

    async def test_cleanup_rejects_changed_content_and_unindexed_files(self):
        first = await self.fetch()
        first["path"].write_bytes(b"changed")
        with self.assertRaises(UCloudAPIError):
            await self.store.remove_delivered(first["path"], first["sha256"])
        self.assertTrue(first["path"].exists())
        unknown = self.root / "keep.txt"
        unknown.write_bytes(b"keep")
        with self.assertRaises(UCloudAPIError):
            await self.store.remove_delivered(unknown, hashlib.sha256(b"keep").hexdigest())
        self.assertTrue(unknown.exists())

    async def test_cleanup_never_deletes_outside_download_root(self):
        outside = Path(self.temp.name) / "keep.txt"
        outside.write_bytes(b"keep")
        with self.assertRaises(UCloudAPIError):
            await self.store.remove_delivered(outside, hashlib.sha256(b"keep").hexdigest())
        self.assertTrue(outside.exists())

    async def test_repeated_download_verifies_hash_without_network(self):
        first = await self.fetch()
        second = await self.fetch()
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["path"], second["path"])
        self.store.transfer.assert_awaited_once()
        self.get_url.assert_awaited_once()
        self.assertEqual(first["path"].stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b"private", (self.root / "downloads.sqlite3").read_bytes())

    async def test_cache_survives_plugin_restart(self):
        first = await self.fetch()
        self.store = DownloadStore(self.root, max_bytes=4096, min_free=0)
        self.store.transfer = AsyncMock(side_effect=AssertionError("must reuse"))
        second = await self.fetch()
        self.assertEqual(first["path"], second["path"])
        self.assertTrue(second["reused"])

    async def test_concurrent_duplicate_only_downloads_once(self):
        results = await asyncio.gather(*(self.fetch() for _ in range(5)))
        self.assertEqual(sum(not item["reused"] for item in results), 1)
        self.store.transfer.assert_awaited_once()

    async def test_same_content_different_resource_only_keeps_one_file(self):
        first = await self.fetch()
        second = await self.fetch(id="2", name="renamed.pdf")
        self.assertTrue(second["reused"])
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(len(list(self.root.rglob("*.pdf"))), 1)

    async def test_updated_same_name_retains_both_versions(self):
        first = await self.fetch()
        self.data = self.data.replace(b"fixture", b"updated")
        second = await self.fetch(updateTime="v2")
        self.assertNotEqual(first["path"], second["path"])
        self.assertFalse(second["reused"])
        self.assertNotEqual(first["path"].read_bytes(), second["path"].read_bytes())

    async def test_corruption_same_size_triggers_repair(self):
        first = await self.fetch()
        first["path"].write_bytes(b"x" * len(self.data))
        second = await self.fetch()
        self.assertFalse(second["reused"])
        self.assertEqual(second["path"].read_bytes(), self.data)
        self.assertEqual(self.store.transfer.await_count, 2)

    async def test_missing_file_downloads_again(self):
        first = await self.fetch()
        first["path"].unlink()
        second = await self.fetch()
        self.assertTrue(second["path"].exists())
        self.assertEqual(self.store.transfer.await_count, 2)

    async def test_accounts_and_sessions_do_not_share_cache(self):
        first = await self.fetch()
        second = await self.fetch(scope="account-two")
        self.assertFalse(second["reused"])
        self.assertNotEqual(first["path"], second["path"])

    async def test_unknown_revision_refetches_and_content_deduplicates(self):
        await self.fetch(updateTime=None)
        result = await self.fetch(updateTime=None)
        self.assertTrue(result["reused"])
        self.assertEqual(self.store.transfer.await_count, 2)

    async def test_truncated_content_not_indexed_or_left_as_partial(self):
        with self.assertRaises(UCloudAPIError):
            await self.fetch(fileSize=len(self.data) + 1)
        self.assertFalse(list(self.root.rglob(".download.part")))
        db = self.store.database()
        self.assertEqual(db.execute("SELECT count(*) FROM resources").fetchone()[0], 0)
        db.close()

    async def test_cancellation_cleans_partial_and_can_retry(self):
        started = asyncio.Event()
        async def blocked(url, output):
            output.write(b"partial")
            started.set()
            await asyncio.Event().wait()
        self.store.transfer.side_effect = blocked
        task = asyncio.create_task(self.fetch())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(list(self.root.rglob(".download.part")))
        self.assertFalse(self.store.lock.locked())

    async def test_oversize_and_quota_fail_before_network(self):
        with self.assertRaises(UCloudAPIError):
            await self.fetch(fileSize=4097)
        self.store.max_storage = 1
        with self.assertRaises(UCloudAPIError):
            await self.fetch()
        self.get_url.assert_not_awaited()

    async def test_symlink_cache_is_not_reused(self):
        first = await self.fetch()
        outside = Path(self.temp.name) / "outside.pdf"
        outside.write_bytes(self.data)
        first["path"].unlink()
        first["path"].symlink_to(outside)
        second = await self.fetch()
        self.assertFalse(second["reused"])
        self.assertFalse(second["path"].is_symlink())
        self.assertEqual(outside.read_bytes(), self.data)

    async def test_server_sha_and_safe_name(self):
        with self.assertRaises(UCloudAPIError):
            await self.fetch(sha256="0" * 64)
        result = await self.fetch(name="../../教案.pdf", sha256=hashlib.sha256(self.data).hexdigest())
        self.assertTrue(result["path"].resolve().is_relative_to(self.root.resolve()))
        self.assertNotIn("..", result["path"].name)

    def test_file_format_validation_rejects_html_pdf_and_bad_zip(self):
        path = Path(self.temp.name) / "test"
        for content, ext in [(b"<html>login</html>", "pdf"), (b"%PDF-no-end", "pdf"), (b"broken", "docx")]:
            path.write_bytes(content)
            with self.subTest(ext=ext), self.assertRaises(UCloudAPIError):
                validate_file(path, {"ext": ext})

    def test_zip_crc_and_office_structure(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "unique-text")
        path = Path(self.temp.name) / "test.docx"
        path.write_bytes(data.getvalue())
        validate_file(path, {"ext": "docx"})
        path.write_bytes(data.getvalue().replace(b"unique-text", b"broken-text"))
        with self.assertRaises(UCloudAPIError):
            validate_file(path, {"ext": "docx"})

    def test_url_allowlist(self):
        for url in ["http://fileucloud.bupt.edu.cn/a", "https://127.0.0.1/a", "https://fileucloud.bupt.edu.cn.evil/a", "https://user@fileucloud.bupt.edu.cn/a", "https://fileucloud.bupt.edu.cn:8443/a"]:
            with self.subTest(url=url), self.assertRaises(UCloudAPIError):
                checked_url(url)

    async def test_dns_private_addresses_blocked(self):
        resolver = PublicResolver()
        resolver.delegate = Mock(resolve=AsyncMock(return_value=[{"host": "127.0.0.1"}]), close=AsyncMock())
        with self.assertRaises(UCloudAPIError):
            await resolver.resolve("fileucloud.bupt.edu.cn")
        await resolver.close()

    async def test_campus_exception_is_exact_host_and_address(self):
        resolver = PublicResolver()
        resolver.delegate = Mock(resolve=AsyncMock(return_value=[{"host": "10.3.19.2"}]), close=AsyncMock())
        self.assertEqual((await resolver.resolve("fileucloud.bupt.edu.cn"))[0]["host"], "10.3.19.2")
        with self.assertRaises(UCloudAPIError):
            await resolver.resolve("apiucloud.bupt.edu.cn")
        resolver.delegate.resolve.return_value = [{"host": "10.3.19.3"}]
        with self.assertRaises(UCloudAPIError):
            await resolver.resolve("fileucloud.bupt.edu.cn")
        await resolver.close()


class ResourceAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_student_tree_and_attachment_deduplication(self):
        client = DirectUCloudClient()
        client._json_request = AsyncMock(return_value={"data": [{"attachmentVOs": [{"resource": {"id": "1"}}], "children": [{"attachmentVOs": [{"resource": {"id": "1"}}, {"resource": {"id": "2"}}]}]}]})
        resources = await client.get_course_resources({"access_token": "private"}, "course")
        self.assertEqual([x["id"] for x in resources], ["1", "2"])
        self.assertIn("/tree/student", client._json_request.call_args.args[1])

    async def test_metadata_requires_all_requested_resources(self):
        client = DirectUCloudClient()
        client._json_request = AsyncMock(return_value={"data": [{"id": "1"}]})
        with self.assertRaises(UCloudAPIError):
            await client.get_resource_metadata({"access_token": "private"}, ["1", "2"])

    async def test_metadata_batches_and_uses_original_download_endpoint(self):
        client = DirectUCloudClient()
        client._json_request = AsyncMock(return_value={"data": [{"id": "1"}]})
        result = await client.get_resource_metadata({"access_token": "private"}, ["1", "1"])
        self.assertEqual(len(result), 1)
        client._json_request.return_value = {"data": "https://fileucloud.bupt.edu.cn/original"}
        await client.get_resource_url({"access_token": "private"}, "1")
        self.assertIn("/resource/filePath", client._json_request.call_args.args[1])


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def run_transfer(self, *, status=200, headers=None, length=None, content=b"abc", limit=20):
        class Response:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def iter_chunked(self, size):
                yield content
        response = Response()
        response.status, response.headers, response.content_length = status, headers or {}, length
        response.content = response
        client = Mock(get=Mock(return_value=response))
        session = AsyncMock()
        session.__aenter__.return_value = client
        connector = Mock(close=AsyncMock())
        resolver = Mock(close=AsyncMock())
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(aiohttp, "ClientSession", return_value=session) as factory, \
                patch.object(aiohttp, "TCPConnector", return_value=connector), \
                patch("astrbot_plugin_ucloud.downloads.PublicResolver", return_value=resolver):
            store = DownloadStore(Path(directory), max_bytes=limit)
            output = io.BytesIO()
            await store.transfer("https://fileucloud.bupt.edu.cn/file", output)
            self.assertFalse(factory.call_args.kwargs["trust_env"])
            self.assertEqual(client.get.call_args.kwargs["headers"], {"Accept-Encoding": "identity"})
            self.assertFalse(client.get.call_args.kwargs["allow_redirects"])
            return output.getvalue()

    async def test_success_has_no_auth_headers(self):
        self.assertEqual(await self.run_transfer(length=3), b"abc")

    async def test_truncated_transfer_rejected(self):
        with self.assertRaises(UCloudAPIError):
            await self.run_transfer(length=4)

    async def test_chunked_size_limit_enforced(self):
        with self.assertRaises(UCloudAPIError):
            await self.run_transfer(limit=2)

    async def test_redirect_untrusted_host_rejected(self):
        with self.assertRaises(UCloudAPIError):
            await self.run_transfer(status=302, headers={"Location": "https://evil.example/file"})

    async def test_expired_link_and_encoded_content_rejected(self):
        with self.assertRaises(UCloudAPIError):
            await self.run_transfer(status=403)
        with self.assertRaises(UCloudAPIError):
            await self.run_transfer(headers={"Content-Encoding": "gzip"})


if __name__ == "__main__":
    unittest.main()
