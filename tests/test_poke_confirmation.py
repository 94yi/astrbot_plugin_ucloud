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
        self.temp.cleanup()

    def arm(self):
        result = self.plugin._draft_preview(self.draft, Event())
        self.assertIn("60 秒", result)

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
