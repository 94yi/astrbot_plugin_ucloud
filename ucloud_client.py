"""Direct client for BUPT CAS and Teaching Cloud.

This module has no AstrBot imports so its protocol logic can be tested
independently. It follows YouXam/ucloud and byrdocs/bupt-auth, but talks to
BUPT directly instead of using a public Worker.
"""

from __future__ import annotations

import base64
import html as html_lib
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

SERVICE_URL = "https://ucloud.bupt.edu.cn"
CAS_LOGIN_URL = "https://auth.bupt.edu.cn/authserver/login?service=" + SERVICE_URL
API_BASE_URL = "https://apiucloud.bupt.edu.cn"
PORTAL_AUTH = "Basic cG9ydGFsOnBvcnRhbF9zZWNyZXQ="
DEFAULT_HEADERS = {
    "authorization": PORTAL_AUTH,
    "tenant-id": "000000",
    "referer": f"{SERVICE_URL}/",
}
_CAPTCHA_CONFIG_RE = re.compile(
    r"config\.captcha\s*=\s*\{[^}]*\bid\s*:\s*['\"]",
    re.IGNORECASE | re.DOTALL,
)


class UCloudError(Exception):
    """Base error for direct Teaching Cloud operations."""


class UCloudLoginError(UCloudError):
    """The supplied credentials or cached tokens cannot authenticate."""


class UCloudCaptchaRequired(UCloudLoginError):
    """CAS requested a captcha that must not be sent to an external OCR."""


class UCloudAPIError(UCloudError):
    """A Teaching Cloud endpoint returned an unsuccessful response."""


def _jwt_expired(token: object, *, leeway_seconds: int = 60) -> bool:
    if not isinstance(token, str):
        return True
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        expiry = float(json.loads(decoded).get("exp", 0))
        return expiry <= time.time() + leeway_seconds
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
        return True


def _extract_execution(html: str) -> str:
    match = re.search(
        r"""<input[^>]*name=["']execution["'][^>]*value=["']([^"']+)""",
        html,
        re.IGNORECASE,
    ) or re.search(
        r"""<input[^>]*value=["']([^"']+)["'][^>]*name=["']execution["']""",
        html,
        re.IGNORECASE,
    )
    if not match:
        raise UCloudLoginError("统一认证页面缺少 execution 字段")
    return match.group(1)


def _extract_login_error(html: str) -> str:
    match = re.search(
        r"""<div[^>]*id=["']errorDiv["'][^>]*>[\s\S]*?<p>(.*?)</p>""",
        html,
        re.IGNORECASE,
    )
    if not match:
        return "统一认证登录失败"
    return html_lib.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()


class DirectUCloudClient:
    """Authenticate with BUPT CAS and call Teaching Cloud directly."""

    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self.timeout = httpx.Timeout(timeout_seconds)

    async def _json_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise UCloudAPIError("教学云返回了非 JSON 数据") from exc
        if not isinstance(payload, dict):
            raise UCloudAPIError("教学云返回了无法识别的数据")
        if payload.get("success") is False:
            raise UCloudAPIError(str(payload.get("msg") or "教学云请求失败"))
        return payload

    async def login(self, username: str, password: str) -> dict[str, Any]:
        headers = {
            "user-agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "Chrome/124.0 Safari/537.36"
            )
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            headers=headers,
        ) as client:
            page = await client.get(CAS_LOGIN_URL)
            page.raise_for_status()
            html = page.text
            execution = _extract_execution(html)
            if _CAPTCHA_CONFIG_RE.search(html):
                raise UCloudCaptchaRequired(
                    "统一认证要求验证码；为避免外部 OCR 泄露会话，请稍后重试"
                )
            response = await client.post(
                CAS_LOGIN_URL,
                data={
                    "username": username,
                    "password": password,
                    "submit": "登录",
                    "type": "username_password",
                    "execution": execution,
                    "_eventId": "submit",
                },
                headers={"referer": CAS_LOGIN_URL},
            )
            if response.status_code != 302:
                message = _extract_login_error(response.text)
                if message == "Invalid credentials.":
                    message = "学号或统一认证密码错误"
                raise UCloudLoginError(message)
            location = response.headers.get("location", "")
            ticket = parse_qs(urlparse(location).query).get("ticket", [""])[0]
            if not ticket:
                raise UCloudLoginError("统一认证没有返回 service ticket")

        token_payload = await self._json_request(
            "POST",
            f"{API_BASE_URL}/ykt-basics/oauth/token",
            headers={
                **DEFAULT_HEADERS,
                "accept": "application/json, text/plain, */*",
                "content-type": "application/x-www-form-urlencoded",
            },
            content=urlencode({"ticket": ticket, "grant_type": "third"}),
        )
        roles = await self.get_roles(str(token_payload.get("refresh_token", "")))
        if not roles:
            raise UCloudLoginError("当前账号没有可用的教学云角色")
        refreshed = await self.refresh(token_payload, identity=str(roles[0]["id"]))
        refreshed["roles"] = roles
        refreshed["identity"] = str(roles[0]["id"])
        return refreshed

    async def get_roles(self, token: str) -> list[dict[str, Any]]:
        payload = await self._json_request(
            "GET",
            f"{API_BASE_URL}/ykt-basics/userroledomaindept/listByUserId",
            headers={**DEFAULT_HEADERS, "blade-auth": token},
        )
        roles = payload.get("data", [])
        if not isinstance(roles, list):
            return []
        return [item for item in roles if isinstance(item, dict)]

    async def refresh(
        self,
        userinfo: dict[str, Any],
        *,
        identity: str | None = None,
    ) -> dict[str, Any]:
        refresh_token = str(userinfo.get("refresh_token", ""))
        if not refresh_token:
            raise UCloudLoginError("缺少 refresh token")
        selected_identity = identity or str(userinfo.get("identity", ""))
        fields: dict[str, tuple[None, str]] = {
            "grant_type": (None, "refresh_token"),
            "refresh_token": (None, refresh_token),
        }
        if selected_identity:
            fields["identity"] = (None, selected_identity)
        payload = await self._json_request(
            "POST",
            f"{API_BASE_URL}/ykt-basics/oauth/token",
            headers={"authorization": PORTAL_AUTH},
            files=fields,
        )
        if selected_identity:
            payload["identity"] = selected_identity
        if isinstance(userinfo.get("roles"), list):
            payload["roles"] = userinfo["roles"]
        return payload

    async def ensure_userinfo(
        self,
        username: str,
        password: str,
        cached: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if cached and not _jwt_expired(cached.get("access_token")):
            return cached
        if cached and not _jwt_expired(cached.get("refresh_token")):
            try:
                return await self.refresh(cached)
            except (httpx.HTTPError, UCloudError):
                pass
        return await self.login(username, password)

    @staticmethod
    def _api_headers(userinfo: dict[str, Any]) -> dict[str, str]:
        token = str(userinfo.get("access_token", ""))
        if not token:
            raise UCloudLoginError("缺少 access token")
        return {**DEFAULT_HEADERS, "blade-auth": token}

    async def get_undone_list(self, userinfo: dict[str, Any]) -> dict[str, Any]:
        payload = await self._json_request(
            "GET",
            f"{API_BASE_URL}/ykt-site/site/student/undone",
            headers={
                **self._api_headers(userinfo),
                "identity": "JS005:1528800428957896705",
            },
            params={"userId": str(userinfo.get("user_id", ""))},
        )
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise UCloudAPIError("待办列表结构无效")
        items = data.get("undoneList", [])
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict) or isinstance(
                    item.get("courseInfo"), dict
                ):
                    continue
                item["courseInfo"] = {
                    "id": str(item.get("siteId") or ""),
                    "name": str(item.get("siteName") or "未知课程"),
                    "teachers": "",
                }
        return data

    async def get_homework(
        self, userinfo: dict[str, Any], activity_id: str
    ) -> dict[str, Any]:
        payload = await self._json_request(
            "GET",
            f"{API_BASE_URL}/ykt-site/work/detail",
            headers={
                **self._api_headers(userinfo),
                "identity": "JS005:1528800428957896705",
            },
            params={"assignmentId": activity_id},
        )
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise UCloudAPIError("作业详情结构无效")
        return data

    async def get_submission_state(
        self, userinfo: dict[str, Any], activity_id: str
    ) -> dict[str, Any]:
        """Read the same submission state used by the student assignment page."""
        payload = await self._json_request(
            "GET",
            f"{API_BASE_URL}/ykt-site/work/submit-view",
            headers={
                **self._api_headers(userinfo),
                "identity": "JS005:1528800428957896705",
            },
            params={"assignmentId": activity_id},
        )
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise UCloudAPIError("作业提交状态结构无效")
        return data

    async def get_courses(self, userinfo):
        result = []
        for page in range(1, 21):
            payload = await self._json_request(
                "GET", API_BASE_URL + "/ykt-site/site/list/student/current",
                headers=self._api_headers(userinfo),
                params={"userId": userinfo.get("user_id", ""), "siteRoleCode": 2,
                        "current": page, "size": 100},
            )
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("records"), list):
                raise UCloudAPIError("课程列表格式异常")
            result.extend(data["records"])
            if len(data["records"]) < 100:
                return result
        raise UCloudAPIError("课程列表超过安全分页上限")

    async def get_course_resources(self, userinfo, site_id):
        """Student visibility only; never query the teacher/owner resource tree."""
        payload = await self._json_request(
            "POST", API_BASE_URL + "/ykt-site/site-resource/tree/student",
            headers=self._api_headers(userinfo),
            params={"siteId": site_id, "userId": userinfo.get("user_id", "")},
        )
        tree = payload.get("data")
        if not isinstance(tree, list):
            raise UCloudAPIError("课程资源树格式异常")
        result, visited = {}, 0
        def visit(nodes, depth=0):
            nonlocal visited
            if depth > 20 or not isinstance(nodes, list):
                raise UCloudAPIError("课程资源树超限或格式异常")
            for node in nodes:
                visited += 1
                if visited > 5000 or not isinstance(node, dict):
                    raise UCloudAPIError("课程资源树超限或格式异常")
                for attachment in node.get("attachmentVOs") or []:
                    resource = attachment.get("resource") if isinstance(attachment, dict) else None
                    if isinstance(resource, dict) and resource.get("id"):
                        result[str(resource["id"])] = resource
                visit(node.get("children") or [], depth + 1)
        visit(tree)
        return list(result.values())

    async def get_resource_metadata(self, userinfo, resource_ids):
        ids = list(dict.fromkeys(str(value) for value in resource_ids))
        if len(ids) > 2000 or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) for value in ids):
            raise UCloudAPIError("附件标识无效或数量超限")
        result = {}
        for offset in range(0, len(ids), 50):
            batch = ids[offset:offset + 50]
            payload = await self._json_request(
                "GET", API_BASE_URL + "/blade-source/resource/list/byId",
                headers=self._api_headers(userinfo), params={"resourceIds": ",".join(batch)},
            )
            data = payload.get("data")
            if not isinstance(data, list):
                raise UCloudAPIError("附件信息格式异常")
            for item in data:
                if not isinstance(item, dict) or str(item.get("id")) not in batch:
                    raise UCloudAPIError("附件信息与请求不一致")
                result[str(item["id"])] = item
        if set(result) != set(ids):
            raise UCloudAPIError("部分附件不可访问或已被移除")
        return [result[value] for value in ids]

    async def get_resource_url(self, userinfo, resource_id):
        payload = await self._json_request(
            "GET", API_BASE_URL + "/blade-source/resource/filePath",
            headers=self._api_headers(userinfo), params={"resourceId": resource_id},
        )
        if not isinstance(payload.get("data"), str) or not payload["data"]:
            raise UCloudAPIError("教学云未返回原文件下载地址")
        return payload["data"]

    async def upload_attachment(
        self,
        userinfo: dict[str, Any],
        path: str | Path,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> str:
        """Upload one attachment with the student page's ``bizType=3`` flow."""
        file_path = Path(path)
        if not file_path.is_file():
            raise UCloudAPIError("待上传附件不存在")
        safe_name = Path(filename or file_path.name).name
        guessed_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        with file_path.open("rb") as handle:
            payload = await self._json_request(
                "POST",
                f"{API_BASE_URL}/blade-source/resource/upload/biz",
                headers=self._api_headers(userinfo),
                data={"userId": str(userinfo.get("user_id", "")), "bizType": "3"},
                files={"file": (safe_name, handle, mime_type or guessed_type)},
                timeout=httpx.Timeout(3600.0, connect=30.0),
            )
        resource_id = payload.get("data")
        if resource_id in (None, ""):
            raise UCloudAPIError("教学云没有返回附件资源 ID")
        return str(resource_id)

    async def submit_homework(
        self,
        userinfo: dict[str, Any],
        activity_id: str,
        content: str,
        attachment_ids: list[str],
        *,
        assignment_type: int,
        group_id: str = "",
        commit_id: str = "",
    ) -> dict[str, Any]:
        """Submit using the exact request body emitted by the student page."""
        return await self._json_request(
            "POST",
            f"{API_BASE_URL}/ykt-site/work/submit",
            headers={
                **self._api_headers(userinfo),
                "content-type": "application/json",
                "identity": "JS005:1528800428957896705",
            },
            json={
                "attachmentIds": attachment_ids,
                "assignmentContent": content.rstrip(),
                "assignmentId": activity_id,
                "assignmentType": assignment_type,
                "userId": str(userinfo.get("user_id", "")),
                "groupId": group_id if assignment_type == 1 else "",
                "commitId": commit_id,
            },
        )
