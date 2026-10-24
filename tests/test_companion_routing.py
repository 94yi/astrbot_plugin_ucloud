"""Optional integration with the actual AstrBot waiter and Companion I/O guard."""

import asyncio
from contextlib import suppress
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_ucloud.main import Main


class CompanionWaiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_waiter_selection_routes_through_guard_without_submission(self):
        guard_path = os.environ.get("COMPANION_GUARD_TEST_PATH")
        if not guard_path:
            self.skipTest("Companion guard integration is optional")
        from astrbot.core.utils.session_waiter import USER_SESSIONS

        spec = importlib.util.spec_from_file_location(
            "isolated_companion_guard", guard_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Event:
            unified_msg_origin = "isolated-picker"

            def __init__(self, text):
                self.message_str, self.stopped = text, False

            def get_platform_name(self):
                return "aiocqhttp"

            def get_self_id(self):
                return "123"

            def get_sender_id(self):
                return "456"

            def plain_result(self, text):
                return text

            def should_call_llm(self, value):
                self.llm = value

            def stop_event(self):
                self.stopped = True

            def is_stopped(self):
                return self.stopped

            def get_extra(self, _key):
                return []

        with tempfile.TemporaryDirectory() as directory:
            plugin = object.__new__(Main)
            plugin._store_path = Path(directory) / "accounts.json"
            plugin._drafts, plugin._task_lists = {}, {}
            plugin._account = AsyncMock(return_value={"username": "student"})
            plugin._fetch = AsyncMock(
                return_value={
                    "undoneList": [
                        {
                            "activityId": "assignment-1",
                            "activityName": "隔离测试作业",
                            "siteName": "测试课程",
                        }
                    ]
                }
            )
            plugin._prepare_submission_draft = AsyncMock(
                return_value="草稿预览，需要确认码"
            )
            plugin._client = SimpleNamespace(submit_homework=AsyncMock())
            plugin.context = SimpleNamespace(send_message=AsyncMock())
            generator = plugin.begin_submit(Event("/ucloud_submit"))
            prompt = await anext(generator)
            self.assertIn("请选择", prompt)
            finish = asyncio.create_task(anext(generator))
            try:
                for _ in range(30):
                    if "isolated-picker" in USER_SESSIONS:
                        break
                    await asyncio.sleep(0)
                self.assertIn("isolated-picker", USER_SESSIONS)
                choice = Event("1")
                await module.CompanionIoGuard.platform_only(SimpleNamespace(), choice)
                with self.assertRaises(StopAsyncIteration):
                    await finish
                self.assertTrue(choice.stopped)
                self.assertFalse(choice.llm)
                plugin._prepare_submission_draft.assert_awaited_once_with(
                    choice, "assignment-1"
                )
                plugin._client.submit_homework.assert_not_awaited()
                self.assertNotIn("isolated-picker", USER_SESSIONS)
                self.assertFalse(
                    list((Path(directory) / "companion-routing").glob("*.json"))
                )
            finally:
                if not finish.done():
                    finish.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await finish
                await generator.aclose()
