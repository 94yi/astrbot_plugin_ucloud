import base64
import json
import time
import unittest
from unittest.mock import AsyncMock, Mock

import httpx

from astrbot_plugin_ucloud.ucloud_client import (
    DirectUCloudClient,
    UCloudLoginError,
    UCloudUpstreamError,
    _choose_identity,
    _extract_execution,
    _extract_login_fields,
    _extract_login_error,
    _ticket_from_location,
    _CAPTCHA_CONFIG_RE,
    _jwt_expired,
)


def _token(expiry: float) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expiry}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{payload}.signature"


class DirectUCloudClientTests(unittest.IsolatedAsyncioTestCase):
    def test_jwt_expiry_respects_leeway(self) -> None:
        self.assertFalse(_jwt_expired(_token(time.time() + 3600)))
        self.assertTrue(_jwt_expired(_token(time.time() + 30)))
        self.assertTrue(_jwt_expired("not-a-token"))

    def test_extract_execution_supports_both_attribute_orders(self) -> None:
        self.assertEqual(
            _extract_execution('<input name="execution" value="abc">'), "abc"
        )
        self.assertEqual(
            _extract_execution('<input value="def" name="execution">'), "def"
        )

    def test_extract_execution_rejects_changed_login_page(self) -> None:
        with self.assertRaises(UCloudLoginError):
            _extract_execution("<html></html>")

    def test_extract_login_fields_preserves_dynamic_cas_fields(self) -> None:
        fields = _extract_login_fields(
            '<input value="abc" name="execution">'
            '<input type="hidden" name="type" value="dynamic_type">'
            '<input type="hidden" name="_eventId" value="proceed">'
            '<input type="file" name="upload" value="ignored">'
        )
        self.assertEqual(
            fields,
            {"execution": "abc", "type": "dynamic_type", "_eventId": "proceed"},
        )

    def test_ticket_callback_must_match_https_ucloud_service(self) -> None:
        self.assertEqual(
            _ticket_from_location("https://ucloud.bupt.edu.cn/?ticket=ST-123"),
            "ST-123",
        )
        self.assertEqual(
            _ticket_from_location("https://evil.example/?ticket=ST-secret"), ""
        )
        self.assertEqual(
            _ticket_from_location("http://ucloud.bupt.edu.cn/?ticket=ST-secret"), ""
        )

    def test_choose_identity_keeps_previous_role_when_available(self) -> None:
        roles = [{"id": "first"}, {"id": "preferred"}]
        self.assertEqual(_choose_identity(roles, "preferred"), "preferred")
        self.assertEqual(_choose_identity(roles, "missing"), "first")

    def test_extract_login_error_removes_markup(self) -> None:
        html = (
            '<div class="alert" id="errorDiv">\n'
            '  <span>提示</span>\n<p>Bad <b>password</b> &amp; retry</p>\n</div>'
        )
        self.assertEqual(_extract_login_error(html), "Bad password & retry")

    def test_captcha_config_supports_spacing_and_multiline(self) -> None:
        html = """
        <script>
        config.captcha = {
            id: 'captcha'
        }
        </script>
        """
        self.assertIsNotNone(_CAPTCHA_CONFIG_RE.search(html))
    async def test_undone_list_normalizes_course_info(self) -> None:
        client = DirectUCloudClient()

        async def fake_request(*_args, **_kwargs):
            return {
                "success": True,
                "data": {
                    "undoneList": [
                        {"activityId": "1", "siteId": 7, "siteName": "测试课程"}
                    ]
                },
            }

        client._json_request = fake_request  # type: ignore[method-assign]
        data = await client.get_undone_list(
            {"access_token": "token", "user_id": "user"}
        )
        self.assertEqual(data["undoneList"][0]["courseInfo"]["name"], "测试课程")

    async def test_safe_get_retries_one_connection_failure(self) -> None:
        client = DirectUCloudClient()
        request = httpx.Request("GET", "https://auth.bupt.edu.cn/")
        response = httpx.Response(200, request=request)
        upstream = Mock()
        upstream.get = AsyncMock(
            side_effect=[httpx.ConnectError("offline", request=request), response]
        )

        result = await client._get_with_retry(
            upstream, str(request.url), stage="cas-get"
        )

        self.assertIs(result, response)
        self.assertEqual(upstream.get.await_count, 2)

    async def test_safe_get_exposes_only_stage_after_retry_exhaustion(self) -> None:
        client = DirectUCloudClient()
        request = httpx.Request("GET", "https://auth.bupt.edu.cn/")
        upstream = Mock()
        upstream.get = AsyncMock(
            side_effect=httpx.ConnectError("secret transport detail", request=request)
        )

        with self.assertRaises(UCloudUpstreamError) as raised:
            await client._get_with_retry(
                upstream, str(request.url), stage="cas-get"
            )

        self.assertEqual(raised.exception.stage, "cas-get")
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(upstream.get.await_count, 2)


if __name__ == "__main__":
    unittest.main()
