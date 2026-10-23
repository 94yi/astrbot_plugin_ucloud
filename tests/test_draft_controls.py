"""Natural-language draft operations use real handlers with mocked transport."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot_plugin_ucloud.submission_tool import manage_submission

SCOPE = {"subject_id": "456", "bot_id": "123"}


def payload(operation, **kwargs):
    return {"source_id": "source_test", "message_id": "100", "operation": operation,
            "text": "", "attachment_index": 0, "use_quoted_files": False, **kwargs}


class DraftControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_poke_confirmation import PokeConfirmationTests, Event
        PokeConfirmationTests.setUp(self)
        self.event = Event()
        self.plugin.context = SimpleNamespace(send_message=AsyncMock())
        self.verified = patch("astrbot_plugin_ucloud.submission_tool._verified_event", AsyncMock(return_value=self.event))
        self.verified.start()

    def tearDown(self):
        from test_poke_confirmation import PokeConfirmationTests
        self.verified.stop()
        PokeConfirmationTests.tearDown(self)

    async def call(self, operation, **kwargs):
        return await manage_submission(self.plugin, payload(operation, **kwargs), SCOPE, self.event.unified_msg_origin)

    def no_submit(self):
        self.plugin._client.upload_attachment.assert_not_awaited()
        self.plugin._client.submit_homework.assert_not_awaited()

    async def test_preview_edit_literal_dash_delete_and_cancel_use_real_handlers(self):
        old = self.draft["code"]
        self.assertTrue((await self.call("preview"))["preview_sent"])
        self.assertNotEqual(old, self.draft["code"])
        self.assertTrue((await self.call("set_content", text="-"))["prepared"])
        self.assertEqual(self.draft["content"], "-")
        self.draft["attachments"] = [{"name": "answer.pdf", "resource_id": "remote-1"}]
        self.assertTrue((await self.call("remove_attachment", attachment_index=1))["prepared"])
        self.assertEqual(self.draft["attachments"], [])
        result = await self.call("cancel")
        self.assertTrue(result["cancelled"])
        self.assertFalse(self.plugin._drafts)
        self.assertFalse(result["submitted"])
        self.no_submit()

    async def test_duplicate_operation_does_not_rearm_or_modify_changed_draft(self):
        one = await self.call("preview")
        self.assertEqual(await self.call("preview"), one)
        self.plugin.context.send_message.assert_awaited_once()
        with self.assertRaises(ValueError):
            await self.call("preview", text="inject")
        self.draft["code"] = "CHANGED"
        result = await self.call("preview")
        self.assertFalse(result["prepared"])
        self.assertIn("旧操作未重复", result["text"])
        self.plugin.context.send_message.assert_awaited_once()
        self.no_submit()

    async def test_missing_files_do_not_open_waiter_and_wrong_index_does_not_edit(self):
        self.event.get_messages = lambda: []
        with self.assertRaises(ValueError):
            await self.call("add_attachments")
        with self.assertRaises(ValueError):
            await self.call("remove_attachment", attachment_index=2)
        self.assertEqual(self.draft["code"], "ABC123")
        self.plugin.context.send_message.assert_not_awaited()
        self.no_submit()

    async def test_add_attachment_owns_file_and_deduplicates(self):
        from astrbot.api.message_components import Image
        source = Path(self.temp.name) / "answer.png"
        source.write_bytes(b"test-image")
        self.event.get_messages = lambda: [Image.fromFileSystem(str(source))]
        result = await self.call("add_attachments", use_quoted_files=True)
        self.assertTrue(result["preview_sent"])
        self.assertEqual(await self.call("add_attachments", use_quoted_files=True), result)
        self.assertEqual(len(self.draft["attachments"]), 1)
        owned = Path(self.draft["attachments"][0]["path"])
        self.assertTrue(owned.exists())
        self.assertTrue((await self.call("cancel"))["cancelled"])
        self.assertFalse(owned.exists())
        self.assertTrue(source.exists())
        self.no_submit()

    async def test_no_draft_expired_account_changed_or_submit_operation_cannot_write(self):
        with self.assertRaises(ValueError):
            await self.call("confirm")
        self.plugin._account.return_value = {"username": "another"}
        with self.assertRaises(ValueError):
            await self.call("preview")
        self.plugin._account.return_value = {"username": "student"}
        self.draft["expires_at"] = 0
        self.assertFalse((await self.call("preview"))["prepared"])
        self.assertTrue((await self.call("cancel"))["cancelled"])
        self.assertFalse((await self.call("cancel"))["submitted"])
        self.no_submit()

    async def test_edit_waits_for_actual_submission_lock(self):
        key = self.event.unified_msg_origin
        lock = self.plugin._submit_locks.setdefault(key, asyncio.Lock())
        await lock.acquire()
        task = asyncio.create_task(self.call("cancel"))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertIn(key, self.plugin._drafts)
        lock.release()
        self.assertTrue((await task)["cancelled"])
        self.no_submit()

    async def test_failed_preview_delivery_disarms_confirmation(self):
        self.plugin.context.send_message.side_effect = RuntimeError("mock delivery failure")
        with self.assertRaises(RuntimeError):
            await self.call("preview")
        self.assertFalse(self.plugin._poke_windows)
        self.no_submit()


    async def test_preview_returns_only_safe_names_and_indices_for_model_selection(self):
        self.draft["attachments"] = [
            {"name": "wrong.pdf", "path": "/private/original", "resource_id": "secret-id"},
            {"name": "answer.pdf", "resource_id": "remote-id"},
        ]
        result = await self.call("preview")
        self.assertEqual(result["draft"]["attachments"], [{"index": 1, "name": "wrong.pdf"}, {"index": 2, "name": "answer.pdf"}])
        self.assertNotIn("account_username", result["draft"])
        self.assertNotIn("code", result["draft"])
        self.assertNotIn("path", str(result))
        result = await self.call("remove_attachment", attachment_index=1)
        self.assertEqual(result["draft"]["attachments"], [{"index": 1, "name": "answer.pdf"}])
        self.no_submit()


    async def test_preview_replay_rejects_expired_draft_and_unrotated_content_change(self):
        from copy import deepcopy
        await self.call("preview")
        original = deepcopy(self.draft)
        for changes in ({"expires_at": 0}, {"content": "different body"}):
            self.plugin._drafts[self.event.unified_msg_origin] = {**deepcopy(original), **changes}
            self.assertFalse((await self.call("preview"))["prepared"])
        self.plugin.context.send_message.assert_awaited_once()
        self.no_submit()

    async def test_preview_replay_rejects_expired_or_invalidated_confirmation_window(self):
        await self.call("preview")
        window = self.plugin._poke_windows[self.event.unified_msg_origin]
        original_expiry = window["expires"]
        window["expires"] = 0
        self.assertFalse((await self.call("preview"))["prepared"])
        window["expires"] = original_expiry
        window["ready"] = False
        self.assertFalse((await self.call("preview"))["prepared"])
        self.plugin.context.send_message.assert_awaited_once()
        self.no_submit()

    async def test_preview_replay_checks_account_again(self):
        await self.call("preview")
        self.plugin._account.return_value = {"username": "changed"}
        with self.assertRaises(ValueError):
            await self.call("preview")
        self.plugin.context.send_message.assert_awaited_once()
        self.no_submit()


    async def test_preview_changed_during_delivery_never_returns_old_success_metadata(self):
        async def changed_during_delivery(*_args):
            self.draft["content"] = "changed during send"
        self.plugin.context.send_message.side_effect = changed_during_delivery
        result = await self.call("preview")
        self.assertFalse(result["prepared"])
        self.assertNotIn("draft", result)
        self.assertFalse((await self.call("preview"))["prepared"])
        self.plugin.context.send_message.assert_awaited_once()
        self.no_submit()
