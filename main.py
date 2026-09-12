"""BUPT Teaching Cloud integration for AstrBot.

The plugin talks to the public API maintained by YouXam/ucloud and keeps each
user's credentials only in this AstrBot instance's local plugin data directory.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.core.star.star_tools import StarTools


class Main(star.Star):
    """Expose BUPT Teaching Cloud tasks as opt-in AstrBot commands."""

    def __init__(self, context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._store_path: Path | None = None
        self._store_lock = asyncio.Lock()
        self._poll_task: asyncio.Task[None] | None = None

    async def initialize(self) -> None:
        """Prepare local storage and start optional new-task polling."""
        self._store_path = StarTools.get_data_dir(self.name) / "accounts.json"
        if self.config.get("push_enabled", True):
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def terminate(self) -> None:
        """Stop the polling task when AstrBot unloads this plugin."""
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    def _api_base_url(self) -> str:
        """Return a normalized UCloud API base URL."""
        return str(
            self.config.get("api_base_url", "https://ucloud.youxam.workers.dev")
        ).rstrip("/")

    async def _read_accounts(self) -> dict[str, dict[str, Any]]:
        """Load validated account records from local persistent storage."""
        if self._store_path is None or not self._store_path.exists():
            return {}
        try:
            raw = await asyncio.to_thread(self._store_path.read_text, encoding="utf-8")
            loaded = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Unable to read UCloud account store: %s", exc)
            return {}
        if not isinstance(loaded, dict):
            return {}
        return {
            str(key): value
            for key, value in loaded.items()
            if isinstance(value, dict)
            and isinstance(value.get("username"), str)
            and isinstance(value.get("password"), str)
            and isinstance(value.get("session"), str)
        }

    async def _write_accounts(self, accounts: dict[str, dict[str, Any]]) -> None:
        """Persist account records atomically.

        Args:
            accounts: Account records keyed by AstrBot unified message origin.
        """
        if self._store_path is None:
            raise RuntimeError("UCloud account storage is not initialized")
        temporary = self._store_path.with_suffix(".tmp")
        content = json.dumps(accounts, ensure_ascii=False, indent=2)
        await asyncio.to_thread(temporary.write_text, content, encoding="utf-8")
        await asyncio.to_thread(temporary.replace, self._store_path)

    async def _fetch(self, account: dict[str, Any], path: str) -> Any:
        """Request one authenticated UCloud API resource.

        Args:
            account: Local account record containing the username and password.
            path: API path, including an optional query string.

        Returns:
            Decoded JSON response.

        Raises:
            httpx.HTTPStatusError: If the UCloud API rejects the request.
            ValueError: If the API returns non-JSON data.
        """
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.get(
                f"{self._api_base_url()}{path}",
                auth=(account["username"], account["password"]),
                headers={"Accept": "application/json"},
            )
        response.raise_for_status()
        return response.json()

    async def _poll_loop(self) -> None:
        """Periodically send opt-in notices for newly appearing assignments."""
        while True:
            interval = max(60, int(self.config.get("push_interval_seconds", 900)))
            await asyncio.sleep(interval)
            async with self._store_lock:
                accounts = await self._read_accounts()
                changed = False
                for key, account in accounts.items():
                    if not account.get("push", True):
                        continue
                    try:
                        data = await self._fetch(account, "/undoneList")
                    except (httpx.HTTPError, ValueError) as exc:
                        logger.warning("Unable to poll UCloud tasks for %s: %s", key, exc)
                        continue
                    tasks = data.get("undoneList", []) if isinstance(data, dict) else []
                    if not isinstance(tasks, list):
                        continue
                    current_ids = {
                        str(task.get("activityId"))
                        for task in tasks
                        if isinstance(task, dict) and task.get("activityId") is not None
                    }
                    previous_ids = set(account.get("known_task_ids", []))
                    new_tasks = [
                        task
                        for task in tasks
                        if isinstance(task, dict)
                        and str(task.get("activityId")) not in previous_ids
                    ]
                    account["known_task_ids"] = sorted(current_ids)
                    changed = True
                    if not new_tasks:
                        continue
                    text = "发现新的教学云待办：\n" + "\n".join(
                        self._format_task(task) for task in new_tasks[:5]
                    )
                    if len(new_tasks) > 5:
                        text += f"\n…另有 {len(new_tasks) - 5} 项，请使用 /ucloud_tasks 查看。"
                    try:
                        await self.context.send_message(
                            account["session"], MessageChain([Plain(text)])
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Unable to deliver UCloud notification: %s", exc)
                if changed:
                    await self._write_accounts(accounts)

    def _format_task(self, task: dict[str, Any]) -> str:
        """Render a compact task summary for a command reply or notification."""
        course = task.get("courseInfo") if isinstance(task.get("courseInfo"), dict) else {}
        course_name = str(course.get("name") or "未知课程")
        title = str(task.get("activityName") or "未命名待办")
        deadline = str(task.get("endTime") or "未提供")
        activity_id = str(task.get("activityId") or "-")
        return f"• {title}\n  课程：{course_name}｜截止：{deadline}\n  ID：{activity_id}"

    @filter.command("ucloud_login")
    async def login(self, event: AstrMessageEvent, username: str = "", password: str = ""):
        """Verify and save the caller's UCloud account.

        Args:
            event: Current command event.
            username: Student ID used as UCloud Basic Auth username.
            password: Unified-authentication password.
        """
        username, password = username.strip(), password.strip()
        if not username or not password:
            yield event.plain_result("用法：/ucloud_login 学号 统一认证密码")
            return
        candidate = {"username": username, "password": password}
        try:
            data = await self._fetch(candidate, "/undoneList")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                yield event.plain_result("登录失败：学号或统一认证密码不正确。")
            else:
                yield event.plain_result(f"教学云接口返回 HTTP {exc.response.status_code}。")
            return
        except (httpx.HTTPError, ValueError):
            yield event.plain_result("暂时无法连接教学云接口，请稍后重试。")
            return
        tasks = data.get("undoneList", []) if isinstance(data, dict) else []
        known_ids = [
            str(task.get("activityId"))
            for task in tasks
            if isinstance(task, dict) and task.get("activityId") is not None
        ]
        key = event.unified_msg_origin
        async with self._store_lock:
            accounts = await self._read_accounts()
            accounts[key] = {
                "username": username,
                "password": password,
                "session": event.unified_msg_origin,
                "push": True,
                "known_task_ids": known_ids,
            }
            await self._write_accounts(accounts)
        yield event.plain_result(
            f"登录成功，当前有 {len(known_ids)} 项未完成待办；新待办提醒已开启。"
        )

    @filter.command("ucloud_tasks")
    async def tasks(self, event: AstrMessageEvent):
        """List the caller's current unfinished UCloud tasks.

        Args:
            event: Current command event.
        """
        async with self._store_lock:
            account = (await self._read_accounts()).get(event.unified_msg_origin)
        if not account:
            yield event.plain_result("尚未登录。请先使用 /ucloud_login 学号 统一认证密码。")
            return
        try:
            data = await self._fetch(account, "/undoneList")
        except httpx.HTTPStatusError as exc:
            yield event.plain_result(f"获取待办失败（HTTP {exc.response.status_code}），请尝试重新登录。")
            return
        except (httpx.HTTPError, ValueError):
            yield event.plain_result("暂时无法连接教学云接口，请稍后重试。")
            return
        items = data.get("undoneList", []) if isinstance(data, dict) else []
        items = [item for item in items if isinstance(item, dict)]
        if not items:
            yield event.plain_result("当前没有未完成待办。")
            return
        shown = items[:20]
        text = f"当前共有 {len(items)} 项未完成待办：\n" + "\n".join(
            self._format_task(item) for item in shown
        )
        if len(items) > len(shown):
            text += f"\n…其余 {len(items) - len(shown)} 项未显示。"
        yield event.plain_result(text)

    @filter.command("ucloud_detail")
    async def detail(self, event: AstrMessageEvent, activity_id: str = ""):
        """Show a plain-text summary of one homework item's details.

        Args:
            event: Current command event.
            activity_id: Assignment ID returned by ``ucloud_tasks``.
        """
        activity_id = activity_id.strip()
        if not activity_id:
            yield event.plain_result("用法：/ucloud_detail 作业ID")
            return
        async with self._store_lock:
            account = (await self._read_accounts()).get(event.unified_msg_origin)
        if not account:
            yield event.plain_result("尚未登录。请先使用 /ucloud_login 学号 统一认证密码。")
            return
        try:
            data = await self._fetch(account, f"/homework?id={activity_id}")
        except httpx.HTTPStatusError as exc:
            yield event.plain_result(f"获取作业详情失败（HTTP {exc.response.status_code}）。")
            return
        except (httpx.HTTPError, ValueError):
            yield event.plain_result("暂时无法连接教学云接口，请稍后重试。")
            return
        if not isinstance(data, dict):
            yield event.plain_result("教学云返回了无法识别的作业详情。")
            return
        course = data.get("courseInfo") if isinstance(data.get("courseInfo"), dict) else {}
        content = str(data.get("assignmentContent") or "（无文字说明）")
        if len(content) > 3000:
            content = content[:3000] + "\n…内容过长，已截断。"
        text = (
            f"{data.get('assignmentTitle') or '未命名作业'}\n"
            f"课程：{course.get('name') or '未知课程'}\n"
            f"章节：{data.get('chapterName') or '-'}\n"
            f"开始：{data.get('assignmentBeginTime') or '-'}\n"
            f"截止：{data.get('assignmentEndTime') or '-'}\n\n{content}"
        )
        yield event.plain_result(text)

    @filter.command("ucloud_push")
    async def toggle_push(self, event: AstrMessageEvent):
        """Toggle only the caller's new-task notifications.

        Args:
            event: Current command event.
        """
        async with self._store_lock:
            accounts = await self._read_accounts()
            account = accounts.get(event.unified_msg_origin)
            if not account:
                yield event.plain_result("尚未登录。请先使用 /ucloud_login 学号 统一认证密码。")
                return
            account["push"] = not account.get("push", True)
            accounts[event.unified_msg_origin] = account
            await self._write_accounts(accounts)
        yield event.plain_result("新待办提醒已开启。" if account["push"] else "新待办提醒已关闭。")

    @filter.command("ucloud_logout")
    async def logout(self, event: AstrMessageEvent):
        """Remove the caller's locally stored UCloud credentials.

        Args:
            event: Current command event.
        """
        async with self._store_lock:
            accounts = await self._read_accounts()
            if accounts.pop(event.unified_msg_origin, None) is None:
                yield event.plain_result("当前没有已保存的教学云登录信息。")
                return
            await self._write_accounts(accounts)
        yield event.plain_result("已退出登录，并清除了本机保存的教学云账号信息。")
