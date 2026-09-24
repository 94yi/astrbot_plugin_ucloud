import base64
import json
import time
import unittest

from astrbot_plugin_ucloud.ucloud_client import (
    DirectUCloudClient,
    UCloudLoginError,
    _extract_execution,
    _extract_login_error,
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
        self.assertIsNotNone(_CAPTCHA_CONFIG_RE.search(html))
        """
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


if __name__ == "__main__":
    unittest.main()
