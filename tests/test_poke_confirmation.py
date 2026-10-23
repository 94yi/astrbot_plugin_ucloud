"""Gesture confirmation uses fake events and mocked upload/submit APIs only."""

import asyncio
import importlib.util
import itertools
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from astrbot_plugin_ucloud.main import Main


class Event:
    unified_msg_origin = "test:private:456"
    message_str = ""

    def __init__(self, raw=None):
        self.message_obj = SimpleNamespace(raw_message=raw or {})
        self.send = AsyncMock()
        self.stopped = False

    def get_platform_name(self):
        return "aiocqhttp"

    def is_private_chat(self):
        return not self.message_obj.raw_message.get("group_id")

    def get_self_id(self):
        return "123"

    def get_sender_id(self):
        return "456"

    def plain_result(self, text):
        return text

    def stop_event(self):
        self.stopped = True

    def should_call_llm(self, value):
        self.llm = value

    def is_stopped(self):
        return self.stopped

    def get_extra(self, key):
        return []


POKE_IDS = itertools.count()


def poke(**changes):
    return Event(
        dict(
            post_type="notice",
            notice_type="notify",
            sub_type="poke",
            self_id=123,
            user_id=456,
            target_id=123,
            time=int(time.time()),
            notice_id=next(POKE_IDS),
            **changes,
        )
    )


class PokeConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = object.__new__(Main)
        p = self.plugin
        p._store_path = Path(self.temp.name) / "accounts.json"
        p._submit_locks, p._poke_windows, p._confirmation_pokes = {}, {}, {}
        self.draft = dict(
            activity_id="a1",
            title="Fake homework",
            permission="allowed",
            content="Answer",
            attachments=[],
            assignment_type=0,
            assignment_status=1,
            commit_id="",
            code="ABC123",
            account_username="student",
            expires_at=time.monotonic() + 180,
        )
        p._drafts = {Event.unified_msg_origin: self.draft}
        p._account = AsyncMock(return_value={"username": "student"})
        p._userinfo = AsyncMock(return_value={})
        p._client = SimpleNamespace(
            get_submission_state=AsyncMock(
                return_value={
                    "status": 2,
                    "assignmentStatus": 99,
                    "canSubmit": True,
                    "commitId": "",
                }
            ),
            submit_homework=AsyncMock(),
            upload_attachment=AsyncMock(return_value="r1"),
        )

    def tearDown(self):
        for key in list(self.plugin._poke_windows):
            self.plugin._close_poke_window(key)
        for key in list(getattr(self.plugin, "_attachment_windows", {})):
            self.plugin._close_attachment_window(key)
        for path in list(getattr(self.plugin, "_submission_files", {})):
            self.plugin._remove_submission_file(path)
        self.temp.cleanup()

    def arm(self):
        result = self.plugin._draft_preview(self.draft, Event())
        self.assertIn("60 秒", result)

    async def test_empty_preview_direct_image_then_poke_confirms_owned_copy(self):
        from astrbot.api.message_components import Image
        self.draft["content"] = ""
        preview = self.plugin._draft_preview(self.draft, Event())
        self.assertIn("3 分钟内直接发送", preview)
        self.assertNotIn("确认码提交", preview)
        self.assertNotIn("；", preview)
        self.assertTrue(all(len(line) < 90 for line in preview.splitlines()))
        original = Path(self.temp.name) / "answer.png"
        original.write_bytes(b"fake-image-content")
        image = Event({"post_type": "message"})
        image.get_messages = lambda: [Image.fromFileSystem(str(original))]
        guard_path = os.environ.get("COMPANION_GUARD_TEST_PATH")
        if guard_path:
            spec = importlib.util.spec_from_file_location("direct_media_guard", guard_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            guard = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [
                SimpleNamespace(activated=True, root_dir_name="astrbot_plugin_ucloud", star_cls=self.plugin)
            ]))
            await module.CompanionIoGuard.platform_only(guard, image)
            self.assertFalse(image.llm)
        else:
            self.assertTrue(await self.plugin.handle_submission_input(image))
        owned = Path(self.draft["attachments"][0]["path"])
        self.assertNotEqual(owned, original)
        self.assertEqual(owned.stat().st_mode & 0o777, 0o600)
        original.unlink()  # The real scheduler removes its incoming temporary file.
        self.assertEqual(owned.read_bytes(), b"fake-image-content")
        self.assertTrue(image.stopped)
        self.assertIn("附件（1/10）", image.send.await_args.args[0])
        self.assertIn("60 秒", image.send.await_args.args[0])
        self.plugin._client.upload_attachment.assert_not_awaited()
        self.plugin._client.submit_homework.assert_not_awaited()
        self.assertTrue(await self.plugin.handle_submission_confirmation(poke()))
        self.plugin._client.upload_attachment.assert_awaited_once()
        self.plugin._client.submit_homework.assert_awaited_once()
        self.assertFalse(owned.exists())

    async def test_direct_media_without_active_preview_or_wrong_scope_is_ignored(self):
        from astrbot.api.message_components import Image
        image = Event()
        image.get_messages = lambda: [Image.fromFileSystem("/fake.png")]
        self.assertFalse(await self.plugin.handle_submission_input(image))
        self.draft["content"] = ""
        self.plugin._draft_preview(self.draft, Event())
        for changes in ({"group_id": 42}, {}):
            image = Event(changes)
            image.get_messages = lambda: [Image.fromFileSystem("/fake.png")]
            if not changes:
                image.get_sender_id = lambda: "789"
            self.assertFalse(await self.plugin.handle_submission_input(image))
        self.plugin._attachment_windows[Event.unified_msg_origin]["expires"] = 0
        image = Event()
        image.get_messages = lambda: [Image.fromFileSystem("/fake.png")]
        self.assertFalse(await self.plugin.handle_submission_input(image))
        self.plugin._client.upload_attachment.assert_not_awaited()
        self.plugin._client.submit_homework.assert_not_awaited()

    async def test_second_direct_image_refreshes_confirmation_and_cancel_cleans_files(self):
        from astrbot.api.message_components import Image
        self.arm()
        old_code = self.draft["code"]
        source = Path(self.temp.name) / "answer.png"
        source.write_bytes(b"fake-image")
        image = Event({"post_type": "message"})
        image.get_messages = lambda: [Image.fromFileSystem(str(source))]
        for _ in range(2):
            self.assertTrue(await self.plugin.handle_submission_input(image))
        self.assertEqual(len(self.draft["attachments"]), 2)
        self.assertNotEqual(old_code, self.draft["code"])
        owned = [Path(item["path"]) for item in self.draft["attachments"]]
        self.assertTrue(all(path.exists() for path in owned))
        replies = [r async for r in self.plugin.cancel_submission(Event())]
        self.assertIn("已取消", replies[0])
        self.assertTrue(all(not path.exists() for path in owned))
        self.assertTrue(source.exists())
        self.assertFalse(await self.plugin.handle_submission_confirmation(poke()))
        self.plugin._client.submit_homework.assert_not_awaited()

    async def test_preview_then_poke_submits_once_and_consumes_duplicate(self):
        self.arm()
        event = poke()
        self.assertTrue(await self.plugin.handle_submission_confirmation(event))
        self.plugin._client.submit_homework.assert_awaited_once()
        self.assertTrue(event.stopped)
        self.assertIn("提交成功", event.send.await_args.args[0])
        self.assertTrue(await self.plugin.handle_submission_confirmation(event))
        self.assertEqual(self.plugin._client.submit_homework.await_count, 1)
        self.assertEqual(event.send.await_count, 1)

    async def test_unarmed_expired_cancelled_and_changed_draft_never_submit(self):
        self.assertFalse(await self.plugin.handle_submission_confirmation(poke()))
        self.arm()
        self.plugin._poke_windows[Event.unified_msg_origin]["expires"] = 0
        self.assertFalse(await self.plugin.handle_submission_confirmation(poke()))
        self.arm()
        self.plugin._drafts.clear()
        await self.plugin.handle_submission_confirmation(poke())
        self.plugin._drafts[Event.unified_msg_origin] = self.draft
        self.arm()
        self.draft["content"] = "Edited answer"
        event = poke()
        await self.plugin.handle_submission_confirmation(event)
        self.assertIn("草稿已变化", event.send.await_args.args[0])
        self.plugin._client.submit_homework.assert_not_awaited()
        self.plugin._client.upload_attachment.assert_not_awaited()

    async def test_group_wrong_actor_target_bot_and_old_notice_never_submit(self):
        self.arm()
        for changes in (
            {"group_id": 99},
            {"user_id": 789},
            {"sender_id": 789},
            {"target_id": 456},
            {"self_id": 789},
            {"user_id": 123},
            {"time": int(time.time()) - 10},
            {"time": int(time.time()) + 60},
        ):
            event = poke()
            event.message_obj.raw_message.update(changes)
            await self.plugin.handle_submission_confirmation(event)
        self.plugin._client.submit_homework.assert_not_awaited()

    async def test_background_notices_do_not_disarm_confirmation(self):
        self.arm()
        self.assertFalse(await self.plugin.handle_submission_confirmation(
            Event({"post_type": "notice", "notice_type": "notify", "sub_type": "input_status"})
        ))
        self.assertTrue(await self.plugin.handle_submission_confirmation(poke()))
        self.plugin._client.submit_homework.assert_awaited_once()

    async def test_empty_preview_can_be_cancelled_without_command(self):
        self.draft["content"] = ""
        self.plugin._draft_preview(self.draft, Event())
        event = Event({"post_type": "message"})
        event.message_str = "取消"
        self.assertTrue(await self.plugin.handle_submission_input(event))
        self.assertFalse(self.plugin._drafts)
        self.assertFalse(self.plugin._attachment_windows)
        self.plugin._client.submit_homework.assert_not_awaited()

    async def test_ordinary_chat_invalidates_confirmation(self):
        self.arm()
        event = Event({"post_type": "message"})
        self.assertFalse(await self.plugin.handle_submission_confirmation(event))
        await self.plugin.handle_submission_confirmation(poke())
        self.plugin._client.submit_homework.assert_not_awaited()

    async def test_same_notice_cannot_confirm_a_later_preview(self):
        self.arm()
        event = poke()
        await self.plugin.handle_submission_confirmation(event)
        self.plugin._drafts[Event.unified_msg_origin] = self.draft
        self.arm()
        await self.plugin.handle_submission_confirmation(event)
        self.assertEqual(self.plugin._client.submit_homework.await_count, 1)

    async def test_changed_account_and_remote_commit_block_submit(self):
        self.arm()
        self.plugin._account.return_value = {"username": "different"}
        await self.plugin.handle_submission_confirmation(poke())
        self.plugin._account.return_value = {"username": "student"}
        self.arm()
        event = poke()
        event.message_obj.raw_message["extra"] = "another-gesture"
        self.plugin._client.get_submission_state.return_value = {
            "status": 2,
            "assignmentStatus": 99,
            "commitId": "changed",
        }
        await self.plugin.handle_submission_confirmation(event)
        self.plugin._client.submit_homework.assert_not_awaited()

    async def test_concurrent_duplicate_and_edit_do_not_change_confirmed_payload(self):
        self.arm()
        entered, resume = asyncio.Event(), asyncio.Event()

        async def state(*args):
            entered.set()
            await resume.wait()
            return {"status": 2, "assignmentStatus": 99, "commitId": ""}

        self.plugin._client.get_submission_state.side_effect = state
        first = asyncio.create_task(self.plugin.handle_submission_confirmation(poke()))
        await asyncio.wait_for(entered.wait(), 2)
        self.draft["content"], self.draft["code"] = "New answer", "DEF456"
        await self.plugin.handle_submission_confirmation(poke())
        resume.set()
        await first
        self.plugin._client.submit_homework.assert_awaited_once()
        self.assertEqual(
            self.plugin._client.submit_homework.await_args.args[2], "Answer"
        )
        self.assertIs(self.plugin._drafts[Event.unified_msg_origin], self.draft)

    async def test_actual_io_guard_consumes_confirmation_without_waiter_or_llm(self):
        path = os.environ.get("COMPANION_GUARD_TEST_PATH")
        if not path:
            self.skipTest("Optional Companion integration")
        spec = importlib.util.spec_from_file_location("isolated_poke_guard", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.arm()
        guard = SimpleNamespace(
            context=SimpleNamespace(
                get_all_stars=lambda: [
                    SimpleNamespace(
                        activated=True,
                        root_dir_name="astrbot_plugin_ucloud",
                        star_cls=self.plugin,
                    )
                ]
            )
        )
        event = poke()
        await module.CompanionIoGuard.platform_only(guard, event)
        self.assertTrue(event.stopped)
        self.assertFalse(event.llm)
        self.plugin._client.submit_homework.assert_awaited_once()

    async def test_submit_failure_requires_new_preview_and_no_automatic_retry(self):
        from astrbot_plugin_ucloud.ucloud_client import UCloudAPIError

        self.arm()
        self.plugin._client.submit_homework.side_effect = UCloudAPIError("Mock failure")
        event = poke()
        await self.plugin.handle_submission_confirmation(event)
        await self.plugin.handle_submission_confirmation(poke())
        self.assertEqual(self.plugin._client.submit_homework.await_count, 1)
        self.assertIn("提交失败", event.send.await_args.args[0])
        self.assertNotEqual(self.draft["code"], "ABC123")

    async def test_edit_during_account_lookup_does_not_change_confirmed_payload(self):
        self.arm()

        async def account(_event):
            self.draft["content"], self.draft["code"] = "Later edit", "DEF456"
            return {"username": "student"}

        self.plugin._account.side_effect = account
        await self.plugin.handle_submission_confirmation(poke())
        self.assertEqual(
            self.plugin._client.submit_homework.await_args.args[2], "Answer"
        )
        self.assertIs(self.plugin._drafts[Event.unified_msg_origin], self.draft)
