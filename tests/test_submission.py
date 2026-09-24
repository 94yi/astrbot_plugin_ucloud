import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_ucloud.submission import (
    MAX_CONTENT_LENGTH,
    attachment_error,
    command_tail,
    resolve_group_id,
    submission_permission,
)
from astrbot_plugin_ucloud.ucloud_client import API_BASE_URL, DirectUCloudClient


class SubmissionRuleTests(unittest.TestCase):
    def test_command_tail_preserves_content_spaces(self) -> None:
        self.assertEqual(
            command_tail("/ucloud_submit 123  一段 有空格的正文", maxsplit=2),
            ["123", "一段 有空格的正文"],
        )
        self.assertEqual(MAX_CONTENT_LENGTH, 2000)

    def test_permission_matches_student_page_state_machine(self) -> None:
        self.assertEqual(submission_permission({"status": 1, "assignmentStatus": 99})[0], False)
        self.assertEqual(submission_permission({"status": 2, "assignmentStatus": 99})[0], True)
        self.assertEqual(submission_permission({"status": 2, "assignmentStatus": 0})[0], True)
        self.assertEqual(submission_permission({"status": 2, "assignmentStatus": 1})[0], False)
        self.assertEqual(
            submission_permission(
                {"status": 3, "assignmentStatus": 99, "isOvertimeCommit": 0}
            )[0],
            True,
        )
        self.assertEqual(
            submission_permission(
                {"status": 3, "assignmentStatus": 99, "isOvertimeCommit": 1}
            )[0],
            False,
        )

    def test_group_id_uses_student_list_field_first(self) -> None:
        self.assertEqual(
            resolve_group_id(
                {"studentGroupId": "student-group"},
                {"teamId": "detail-team"},
            ),
            "student-group",
        )

    def test_attachment_rules_match_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "answer.pdf"
            path.write_bytes(b"answer")
            self.assertEqual(attachment_error(str(path), "answer.pdf"), "")
            self.assertIn("不支持", attachment_error(str(path), "answer.exe"))


class SubmissionClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_submission_state_uses_submit_view(self) -> None:
        client = DirectUCloudClient()
        seen = {}

        async def fake_request(method, url, **kwargs):
            seen.update(method=method, url=url, kwargs=kwargs)
            return {"success": True, "data": {"assignmentStatus": 99}}

        client._json_request = fake_request  # type: ignore[method-assign]
        state = await client.get_submission_state({"access_token": "token"}, "123")
        self.assertEqual(state["assignmentStatus"], 99)
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["url"], f"{API_BASE_URL}/ykt-site/work/submit-view")
        self.assertEqual(seen["kwargs"]["params"], {"assignmentId": "123"})

    async def test_submit_payload_matches_student_page(self) -> None:
        client = DirectUCloudClient()
        seen = {}

        async def fake_request(method, url, **kwargs):
            seen.update(method=method, url=url, kwargs=kwargs)
            return {"success": True}

        client._json_request = fake_request  # type: ignore[method-assign]
        await client.submit_homework(
            {"access_token": "token", "user_id": "user-1"},
            "assignment-1",
            "answer  \n",
            ["resource-1"],
            assignment_type=1,
            group_id="group-1",
            commit_id="commit-1",
        )
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["url"], f"{API_BASE_URL}/ykt-site/work/submit")
        self.assertEqual(
            seen["kwargs"]["json"],
            {
                "attachmentIds": ["resource-1"],
                "assignmentContent": "answer",
                "assignmentId": "assignment-1",
                "assignmentType": 1,
                "userId": "user-1",
                "groupId": "group-1",
                "commitId": "commit-1",
            },
        )

    async def test_upload_uses_biz_type_three(self) -> None:
        client = DirectUCloudClient()
        seen = {}

        async def fake_request(method, url, **kwargs):
            seen.update(method=method, url=url, kwargs=kwargs)
            self.assertFalse(kwargs["files"]["file"][1].closed)
            return {"success": True, "data": "resource-2"}

        client._json_request = fake_request  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "answer.txt"
            path.write_text("hello", encoding="utf-8")
            resource_id = await client.upload_attachment(
                {"access_token": "token", "user_id": "user-1"}, path
            )
        self.assertEqual(resource_id, "resource-2")
        self.assertEqual(seen["kwargs"]["data"], {"userId": "user-1", "bizType": "3"})


if __name__ == "__main__":
    unittest.main()
