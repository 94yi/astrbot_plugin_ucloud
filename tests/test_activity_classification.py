"""Unknown task-list types must be verified rather than excluded from homework."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from astrbot_plugin_ucloud.main import Main


class Event:
    unified_msg_origin = "test-session"
    message_str = "/ucloud_submit 1"

    def plain_result(self, text):
        return text


class ActivityClassificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = object.__new__(Main)
        p = self.plugin
        p._drafts, p._task_lists = {}, {}
        p._account = AsyncMock(return_value={"username": "test-student"})
        p._userinfo = AsyncMock(return_value={})
        p._client = SimpleNamespace(
            get_homework=AsyncMock(
                return_value={"assignmentTitle": "实验报告", "assignmentType": 0}
            ),
            get_submission_state=AsyncMock(
                return_value={
                    "status": 2,
                    "assignmentStatus": 99,
                    "assignmentContent": "",
                }
            ),
            upload_attachment=AsyncMock(),
            submit_homework=AsyncMock(),
        )
        p._message_attachments = AsyncMock(return_value=[])
        self.item = {
            "activityId": "activity-1",
            "activityName": "实验报告",
            "assignmentType": -1,
            "type": 4,
        }
        p._remember_task_list(Event.unified_msg_origin, [self.item])

    async def test_unknown_list_type_can_prepare_server_verified_homework(self):
        replies = [r async for r in self.plugin.begin_submit(Event())]
        self.assertIn("实验报告", replies[0])
        self.plugin._client.get_homework.assert_awaited_once_with({}, "activity-1")
        self.plugin._client.get_submission_state.assert_awaited_once()
        self.assertIn(Event.unified_msg_origin, self.plugin._drafts)
        self.plugin._client.upload_attachment.assert_not_awaited()
        self.plugin._client.submit_homework.assert_not_awaited()

    async def test_picker_keeps_unknown_activities_for_selected_item_verification(self):
        event = Event()
        event.message_str = "/ucloud_submit"
        self.plugin._fetch = AsyncMock(return_value={"undoneList": [self.item]})
        generator = self.plugin.begin_submit(event)
        try:
            prompt = await anext(generator)
            self.assertIn("实验报告", prompt)
            self.assertIn("课程任务", prompt)
        finally:
            await generator.aclose()
        self.plugin._client.get_homework.assert_not_awaited()

    async def test_unrecognized_detail_never_creates_a_draft_or_reads_submit_state(
        self,
    ):
        self.plugin._client.get_homework.return_value = {}
        replies = [r async for r in self.plugin.begin_submit(Event())]
        self.assertIn("未创建提交草稿", replies[0])
        self.assertNotIn("不是作业", replies[0])
        self.assertFalse(self.plugin._drafts)
        self.plugin._client.get_submission_state.assert_not_awaited()

    async def test_closed_server_state_and_unsupported_actual_type_never_create_drafts(
        self,
    ):
        self.plugin._client.get_submission_state.return_value = {
            "status": 3,
            "assignmentStatus": 99,
            "isOvertimeCommit": 1,
        }
        replies = [r async for r in self.plugin.begin_submit(Event())]
        self.assertIn("不可提交", replies[0])
        self.assertFalse(self.plugin._drafts)
        self.plugin._client.get_submission_state.return_value = {
            "status": 2,
            "assignmentStatus": 99,
        }
        self.plugin._client.get_homework.return_value["assignmentType"] = -1
        replies = [r async for r in self.plugin.begin_submit(Event())]
        self.assertIn("提交类型尚未接入", replies[0])
        self.assertFalse(self.plugin._drafts)
        self.plugin._client.submit_homework.assert_not_awaited()

    async def test_readonly_task_list_does_not_open_or_prompt_submission(self):
        self.plugin._store_lock = asyncio.Lock()
        self.plugin._read_accounts = AsyncMock(
            return_value={Event.unified_msg_origin: {"username": "test-student"}}
        )
        self.plugin._fetch = AsyncMock(return_value={"undoneList": [self.item]})
        self.plugin._prepare_submission_draft = AsyncMock()
        replies = [r async for r in self.plugin.tasks(Event())]
        self.assertIn("实验报告", replies[0])
        self.assertIn("课程任务", replies[0])
        self.assertNotIn("/ucloud_submit", replies[0])
        self.assertNotIn("确认提交", replies[0])
        self.assertFalse(self.plugin._drafts)
        self.plugin._prepare_submission_draft.assert_not_awaited()
