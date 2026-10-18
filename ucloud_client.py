"""Direct client for BUPT CAS and Teaching Cloud.

This module has no AstrBot imports so its protocol logic can be tested
independently. It follows YouXam/ucloud and byrdocs/bupt-auth, but talks to
BUPT directly instead of using a public Worker.
"""

from __future__ import annotations

import asyncio
import base64
import html as html_lib
import json
import mimetypes
import re
import time
from html.parser import HTMLParser
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
_TRANSIENT_READ_STATUSES = {429, 500, 502, 503, 504}


class UCloudError(Exception):
    """Base error for direct Teaching Cloud operations."""


class UCloudLoginError(UCloudError):
    """The supplied credentials or cached tokens cannot authenticate."""


class UCloudCaptchaRequired(UCloudLoginError):
    """CAS requested a captcha that must not be sent to an external OCR."""


class UCloudAPIError(UCloudError):
    """A Teaching Cloud endpoint returned an unsuccessful response."""


class UCloudUpstreamError(UCloudError):
    """A named upstream authentication stage failed without exposing secrets."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


class _LoginFormParser(HTMLParser):
    """Collect ordinary named inputs from the CAS login form."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "input":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        name = values.get("name", "").strip()
        input_type = values.get("type", "text").lower()
        if (
            name
            and len(name) <= 100
            and re.fullmatch(r"[A-Za-z0-9_.-]+", name)
            and input_type not in {"file", "submit", "button", "image"}
        ):
            self.fields[name] = values.get("value", "")


def _extract_login_fields(html: str) -> dict[str, str]:
    parser = _LoginFormParser()
    parser.feed(html)
    if not parser.fields.get("execution"):
        raise UCloudLoginError("统一认证页面缺少 execution 字段")
    return parser.fields


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
    """Compatibility helper retained for callers and tests."""
    return _extract_login_fields(html)["execution"]


def _ticket_from_location(location: str) -> str:
    """Accept tickets only from the configured HTTPS service callback."""
    parsed = urlparse(location)
    if parsed.scheme != "https" or parsed.hostname != "ucloud.bupt.edu.cn":
        return ""
    return parse_qs(parsed.query).get("ticket", [""])[0]


def _choose_identity(
    roles: list[dict[str, Any]], preferred_identity: str | None
) -> str:
    identities = [str(item.get("id", "")) for item in roles if item.get("id")]
    if preferred_identity and preferred_identity in identities:
        return preferred_identity
    return identities[0] if identities else ""


def _retryable_response(method: str, status_code: int) -> bool:
    """Retry only read requests rejected with a known transient status."""
    return method.upper() == "GET" and status_code in _TRANSIENT_READ_STATUSES


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

    async def _get_with_retry(
        self, client: httpx.AsyncClient, url: str, *, stage: str
    ) -> httpx.Response:
        """Retry one idempotent GET once, never credential or ticket POSTs."""
        for attempt in range(2):
            try:
                return await client.get(url)
            except httpx.TransportError as exc:
                if attempt:
                    raise UCloudUpstreamError(
                        stage, f"{stage} 阶段无法连接上游服务"
                    ) from exc
                await asyncio.sleep(0.2)
        raise AssertionError("unreachable")

    async def _json_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        stage: str = "api",
        **kwargs: Any,
    ) -> dict[str, Any]:
        attempts = 3 if method.upper() == "GET" else 1
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            for attempt in range(attempts):
                try:
                    response = await client.request(
                        method, url, headers=headers, **kwargs
                    )
                except httpx.TransportError as exc:
                    if attempt + 1 == attempts:
                        raise UCloudUpstreamError(
                            stage, f"{stage} 阶段无法连接上游服务"
                        ) from exc
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                if _retryable_response(method, response.status_code) and attempt + 1 < attempts:
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                break
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if stage in {"oauth-ticket", "token-refresh"} and response.status_code in {
                400,
                401,
                403,
            }:
                raise UCloudLoginError("教学云登录凭据已失效") from exc
            raise UCloudUpstreamError(
                stage, f"{stage} 阶段返回 HTTP {response.status_code}"
            ) from exc
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise UCloudAPIError("教学云返回了非 JSON 数据") from exc
        if not isinstance(payload, dict):
            raise UCloudAPIError("教学云返回了无法识别的数据")
        if payload.get("success") is False:
            raise UCloudAPIError(str(payload.get("msg") or "教学云请求失败"))
        return payload

    async def login(
        self,
        username: str,
        password: str,
        *,
        preferred_identity: str | None = None,
    ) -> dict[str, Any]:
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
            trust_env=False,
        ) as client:
            page = await self._get_with_retry(client, CAS_LOGIN_URL, stage="cas-get")
            try:
                page.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise UCloudUpstreamError(
                    "cas-get", "统一认证登录页暂时不可用"
                ) from exc
            html = page.text
            form = _extract_login_fields(html)
            if _CAPTCHA_CONFIG_RE.search(html):
                raise UCloudCaptchaRequired(
                    "统一认证要求验证码；为避免外部 OCR 泄露会话，请稍后重试"
                )
            form.update(
                {
                    "username": username,
                    "password": password,
                    "submit": "登录",
                    "type": form.get("type") or "username_password",
                    "_eventId": form.get("_eventId") or "submit",
                }
            )
            try:
                response = await client.post(
                    CAS_LOGIN_URL,
                    data=form,
                    headers={"referer": CAS_LOGIN_URL},
                )
            except httpx.TransportError as exc:
                raise UCloudUpstreamError(
                    "cas-post", "提交统一认证凭据时无法连接上游服务"
                ) from exc
            if response.status_code not in {302, 303}:
                message = _extract_login_error(response.text)
                if message == "Invalid credentials.":
                    message = "学号或统一认证密码错误"
                raise UCloudLoginError(message)
            location = response.headers.get("location", "")
            ticket = _ticket_from_location(location)
            if not ticket:
                raise UCloudLoginError("统一认证返回了无效的教学云跳转地址")

        token_payload = await self._json_request(
            "POST",
            f"{API_BASE_URL}/ykt-basics/oauth/token",
            headers={
                **DEFAULT_HEADERS,
                "accept": "application/json, text/plain, */*",
                "content-type": "application/x-www-form-urlencoded",
            },
            content=urlencode({"ticket": ticket, "grant_type": "third"}),
            stage="oauth-ticket",
        )
        refresh_token = str(token_payload.get("refresh_token", ""))
        if not refresh_token:
            raise UCloudLoginError("教学云登录响应缺少 refresh token")
        roles = await self.get_roles(refresh_token)
        if not roles:
            raise UCloudLoginError("当前账号没有可用的教学云角色")
        selected_identity = _choose_identity(roles, preferred_identity)
        if not selected_identity:
            raise UCloudLoginError("当前账号的教学云角色缺少身份标识")
        refreshed = await self.refresh(token_payload, identity=selected_identity)
        refreshed["roles"] = roles
        refreshed["identity"] = selected_identity
        return refreshed

    async def get_roles(self, token: str) -> list[dict[str, Any]]:
        payload = await self._json_request(
            "GET",
            f"{API_BASE_URL}/ykt-basics/userroledomaindept/listByUserId",
            headers={**DEFAULT_HEADERS, "blade-auth": token},
            stage="role-list",
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
            stage="token-refresh",
        )
        if not payload.get("access_token"):
            raise UCloudLoginError("教学云刷新响应缺少 access token")
        if not payload.get("refresh_token"):
            payload["refresh_token"] = refresh_token
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
            except UCloudLoginError:
                pass
        preferred_identity = str(cached.get("identity", "")) if cached else None
        return await self.login(
            username, password, preferred_identity=preferred_identity or None
        )

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
            stage="task-list",
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
            stage="assignment-detail",
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
                "GET",
                API_BASE_URL + "/ykt-site/site/list/student/current",
                headers=self._api_headers(userinfo),
                params={
                    "userId": userinfo.get("user_id", ""),
                    "siteRoleCode": 2,
                    "current": page,
                    "size": 100,
                },
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
            "POST",
            API_BASE_URL + "/ykt-site/site-resource/tree/student",
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
                    resource = (
                        attachment.get("resource")
                        if isinstance(attachment, dict)
                        else None
                    )
                    if isinstance(resource, dict) and resource.get("id"):
                        result[str(resource["id"])] = resource
                visit(node.get("children") or [], depth + 1)

        visit(tree)
        return list(result.values())

    async def get_resource_metadata(self, userinfo, resource_ids):
        ids = list(dict.fromkeys(str(value) for value in resource_ids))
        if len(ids) > 2000 or any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) for value in ids
        ):
            raise UCloudAPIError("附件标识无效或数量超限")
        result = {}
        for offset in range(0, len(ids), 50):
            batch = ids[offset : offset + 50]
            payload = await self._json_request(
                "GET",
                API_BASE_URL + "/blade-source/resource/list/byId",
                headers=self._api_headers(userinfo),
                params={"resourceIds": ",".join(batch)},
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
            "GET",
            API_BASE_URL + "/blade-source/resource/filePath",
            headers=self._api_headers(userinfo),
            params={"resourceId": resource_id},
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
