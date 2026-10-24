"""Prepare a preview from one authenticated current QQ message; never submit."""

import asyncio
import hashlib
import json
import re
import secrets
import time

from .submission import DRAFT_TTL_SECONDS

from aiocqhttp.event import Event


async def _media(bot, segments, *, quoted, forwards=False, budget=None, reject_nested=False):
    if not isinstance(segments, list) or any(not isinstance(s, dict) for s in segments):
        raise ValueError("引用消息格式无法识别，请单独发送附件")
    budget = budget if budget is not None else {"files": 0, "nodes": 0, "reads": 0}
    if reject_nested and any(s.get("type") == "reply" or s.get("type") == "forward" and not forwards for s in segments):
        raise ValueError("引用中还有嵌套记录，请单独发送要提交的附件")
    result = [s for s in segments if isinstance(s, dict) and s.get("type") in {"image", "file"}]
    budget["files"] += len(result)
    if budget["files"] > 10:
        raise ValueError("附件最多 10 个，请先明确选择要提交的材料")
    if quoted or forwards:
        for segment in segments:
            kind = segment.get("type")
            data = segment.get("data") or {}
            if kind == "reply" and quoted:
                budget["reads"] += 1
                if budget["reads"] > 4:
                    raise ValueError("引用记录过多，请单独发送材料")
                raw = await bot.call_action("get_msg", message_id=int(data["id"]))
                # A quoted record may belong to either participant of this same private chat.
                if raw.get("message_type") != "private" or raw.get("group_id"):
                    raise ValueError("引用不属于当前私聊")
                owner = str(raw.get("user_id") or (raw.get("sender") or {}).get("user_id") or "")
                if owner not in quoted:
                    raise ValueError("引用不属于当前私聊")
                result += await _media(bot, raw.get("message") or [], quoted=False, forwards=True, budget=budget, reject_nested=True)
            elif kind == "forward":
                budget["reads"] += 1
                if budget["reads"] > 4:
                    raise ValueError("引用记录过多，请单独发送材料")
                raw = await bot.call_action("get_forward_msg", message_id=str(data["id"]))
                nodes = raw.get("messages") or []
                budget["nodes"] += len(nodes)
                if budget["nodes"] > 20:
                    raise ValueError("引用记录过多，请单独发送要提交的附件")
                for node in nodes:
                    content = node.get("message") or node.get("content") or []
                    if isinstance(content, list):
                        result += await _media(bot, content, quoted=False, budget=budget, reject_nested=True)
            if len(result) > 10:
                break
    if len(result) > 10:
        raise ValueError("附件最多 10 个，请先明确选择要提交的材料")
    return result


def _draft_summary(draft):
    if not draft:
        return None
    return {
        "title": str(draft.get("title") or ""),
        "content_length": len(str(draft.get("content") or "")),
        "attachments": [{"index": index, "name": str(item.get("name") or "")}
                        for index, item in enumerate(draft.get("attachments", []), 1)],
    }


async def _verified_event(plugin, payload, scope, session, *, include_media=True):
    message_id = payload["message_id"]
    quoted = payload.get("use_quoted_files", False)
    platform = next((p for p in plugin.context.platform_manager.get_insts()
                     if p.meta().name == "aiocqhttp" and p.meta().id == session.split(":")[0]), None)
    if platform is None:
        raise ValueError("当前 QQ 通路不可用")
    raw = await platform.bot.call_action("get_msg", message_id=int(message_id))
    sender = str(raw.get("user_id") or (raw.get("sender") or {}).get("user_id") or "")
    if (raw.get("message_type") != "private" or raw.get("group_id")
            or sender != str(scope.get("subject_id"))
            or str(raw.get("message_id")) != message_id
            or raw.get("self_id") and str(raw["self_id"]) != str(scope.get("bot_id"))):
        raise ValueError("消息不属于当前绑定私聊")
    stamp = raw.get("time")
    if type(stamp) not in (int, float) or not -5 <= time.time() - stamp <= 600:
        raise ValueError("原消息已过期，请重新说明要提交的作业")
    segments = raw.get("message")
    if not isinstance(segments, list):
        raise ValueError("无法核实原始消息附件，请单独发送文件")
    media = await _media(platform.bot, segments, quoted={sender, str(scope.get("bot_id"))} if quoted else False) if include_media else []
    # Convert only platform-verified media. Quoted instructions never become commands.
    raw = {**raw, "post_type": "message", "self_id": scope["bot_id"], "message": []}
    message = await platform.convert_message(Event(raw))
    if message is None:
        raise ValueError("无法读取当前消息")
    # AstrBot's file converter resolves the first segment's file_id. Convert
    # individually so mixed images and several different files stay correct.
    for segment in media:
        if segment.get("type") == "file":
            data = dict(segment.get("data") or {})
            # NapCat history can name the file with `name`; AstrBot's private
            # file converter otherwise only falls back to file/file_name.
            if data.get("name") and not data.get("file_name"):
                data["file_name"] = data["name"]
            segment = {**segment, "data": data}
        converted = await platform.convert_message(Event({**raw, "message": [segment]}))
        if converted is None or len(converted.message) != 1:
            raise ValueError("附件未能完整读取，未创建草稿；请重新发送材料")
        message.message.extend(converted.message)
    event = platform.create_event(message)
    if event.unified_msg_origin != session or not event.is_private_chat():
        raise ValueError("消息会话不匹配")
    return event


def _snapshot(draft):
    if draft is None:
        return None
    return hashlib.sha256(json.dumps(draft, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _receipt(payload, result, draft, *, account_username=None):
    return {"request": dict(payload), "result": result, "snapshot": _snapshot(draft),
            "account_username": account_username, "expires": time.monotonic() + 600}


def _current_receipt(plugin, entry, session):
    draft = plugin._drafts.get(session)
    if (_snapshot(draft) != entry["snapshot"]
            or draft is not None and draft.get("expires_at", 0) <= time.monotonic()):
        return {"prepared": False, "submitted": False, "retryable": False,
                "reason": "draft_changed_or_expired",
                "text": "草稿已变化或过期，旧操作未重复执行；请重新查看预览。"}
    if entry["result"].get("prepared"):
        nonempty = str(draft.get("content") or "").strip() or draft.get("attachments")
        windows = getattr(plugin, "_poke_windows" if nonempty else "_attachment_windows", {})
        window = windows.get(session)
        if not window or window.get("expires", 0) <= time.monotonic() or not window.get("ready", True):
            return {"prepared": False, "submitted": False, "retryable": False,
                    "reason": "preview_window_ended",
                    "text": "旧预览的接收或确认窗口已结束，未重复操作；请重新查看预览后确认。"}
    return entry["result"]


async def _replay(plugin, entry, payload, scope, session):
    result = _current_receipt(plugin, entry, session)
    if result is not entry["result"]:
        return result
    event = await _verified_event(plugin, payload, scope, session, include_media=False)
    draft = plugin._drafts.get(session)
    if draft is not None:
        account = await plugin._account(event)
        expected = entry.get("account_username")
        if expected is None:
            expected = draft.get("account_username")
        if not account or expected is None or str(account.get("username") or "") != expected:
            raise ValueError("账号已变化，旧回执未复用；请重新创建草稿")
    # Account lookup awaited; check the draft and window again before returning metadata.
    return _current_receipt(plugin, entry, session)


async def prepare_submission(plugin, payload, scope, session):
    """Revalidate the platform message and pending task before creating a local draft."""
    reference = payload.get("reference")
    source_id, message_id = payload.get("source_id"), payload.get("message_id")
    course, answer = payload.get("course_name"), payload.get("answer_text")
    quoted = payload.get("use_quoted_files")
    if (not isinstance(reference, str) or not re.fullmatch(r"id:[0-9]{1,32}", reference)
            or not isinstance(source_id, str) or not source_id.startswith("source_") or len(source_id) > 100
            or not isinstance(message_id, str) or not re.fullmatch(r"-?[0-9]{1,20}", message_id)
            or not isinstance(course, str) or len(course) > 200
            or not isinstance(answer, str) or len(answer) > 2000 or type(quoted) is not bool):
        raise ValueError("提交预览参数无效")
    if not hasattr(plugin, "_prepare_tool_lock"):
        plugin._prepare_tool_lock = asyncio.Lock()
        plugin._prepared_tool_sources = {}
    async with plugin._prepare_tool_lock:
        plugin._prepared_tool_sources = {
            k: v for k, v in plugin._prepared_tool_sources.items() if v["expires"] > time.monotonic()
        }
        old = plugin._prepared_tool_sources.get(source_id)
        if old:
            if old["request"] != payload:
                raise ValueError("同一条请求已有预览，请勿重复准备其他材料")
            return await _replay(plugin, old, payload, scope, session)
        current = plugin._drafts.get(session)
        if current and current.get("expires_at", 0) > time.monotonic():
            return {"prepared": False, "submitted": False, "text": "已有提交草稿，请先查看 /ucloud_preview 或 /ucloud_cancel 取消，未覆盖草稿。"}
        event = await _verified_event(plugin, payload, scope, session)
        account = await plugin._account(event)
        if not account:
            raise ValueError("当前账号已退出，请先登录教学云")
        tasks = await plugin._fetch(account, "/undoneList")
        if not any(str(item.get("activityId")) == reference[3:] for item in tasks.get("undoneList", [])
                   if isinstance(item, dict)):
            raise ValueError("未在当前未完成作业中找到该 ID，请重新查询确认")
        preview = await plugin._prepare_submission_draft(event, reference[3:], answer or None, expected_course=course)
        draft = plugin._drafts.get(session)
        if not draft or draft.get("activity_id") != reference[3:]:
            return {"prepared": False, "submitted": False, "text": preview}
        # Bind the receipt to exactly what was previewed, before delivery awaits.
        result = {"prepared": True, "submitted": False, "preview_sent": True,
                  "draft": _draft_summary(draft),
                  "text": "已发送作业预览，等待用户确认；尚未上传或提交。"}
        entry = _receipt(payload, result, draft, account_username=str(account.get("username") or ""))
        try:
            await plugin.context.send_message(session, event.plain_result(preview))
        except Exception:
            plugin._close_poke_window(session)
            plugin._close_attachment_window(session)
            raise
        plugin._prepared_tool_sources[source_id] = entry
        return _current_receipt(plugin, entry, session)



async def manage_submission(plugin, payload, scope, session):
    """Edit only the existing local draft. Final confirmation is never an operation."""
    operation, text = payload.get("operation"), payload.get("text")
    index, quoted = payload.get("attachment_index"), payload.get("use_quoted_files")
    source, mid = payload.get("source_id"), payload.get("message_id")
    if (not isinstance(operation, str) or operation not in {"preview", "cancel", "set_content", "add_attachments", "remove_attachment"}
            or not isinstance(text, str) or len(text) > 2000
            or type(index) is not int or not 0 <= index <= 10 or type(quoted) is not bool
            or operation != "set_content" and text
            or operation != "remove_attachment" and index
            or operation != "add_attachments" and quoted
            or not isinstance(source, str) or not source.startswith("source_") or len(source) > 100
            or not isinstance(mid, str) or not re.fullmatch(r"-?[0-9]{1,20}", mid)):
        raise ValueError("草稿操作参数无效，不支持通过模型确认提交")
    if not hasattr(plugin, "_prepare_tool_lock"):
        plugin._prepare_tool_lock = asyncio.Lock()
        plugin._prepared_tool_sources = {}
    async with plugin._prepare_tool_lock:
        now = time.monotonic()
        plugin._prepared_tool_sources = {k: v for k, v in plugin._prepared_tool_sources.items() if v["expires"] > now}
        cache_key = (source, operation)
        previous = plugin._prepared_tool_sources.get(cache_key)
        if previous:
            if previous["request"] != payload:
                raise ValueError("这条请求的草稿操作已经执行，请勿重复更改")
            return await _replay(plugin, previous, payload, scope, session)
        event = await _verified_event(plugin, payload, scope, session, include_media=operation == "add_attachments")
        if not hasattr(plugin, "_submit_locks"):
            plugin._submit_locks = {}
        async with plugin._submit_locks.setdefault(session, asyncio.Lock()):
            draft = plugin._drafts.get(session)
            if operation != "cancel" and (not draft or draft.get("expires_at", 0) <= time.monotonic()):
                return {"prepared": False, "submitted": False, "text": "当前没有有效提交草稿，请先说明要交的课程和作业。"}
            if operation != "cancel":
                account = await plugin._account(event)
                if not account or draft.get("account_username", str(account.get("username"))) != str(account.get("username")):
                    raise ValueError("账号已变化，未读取或修改旧账号草稿，请重新创建")
            before_code = (draft or {}).get("code")
            if operation == "set_content":
                draft["content"] = text
                draft["code"] = secrets.token_hex(3).upper()
                draft["expires_at"] = time.monotonic() + DRAFT_TTL_SECONDS
                replies = [event.plain_result(plugin._draft_preview(draft, event))]
            else:
                if operation == "preview":
                    generator = plugin.preview_submission(event)
                elif operation == "cancel":
                    generator = plugin.cancel_submission(event)
                elif operation == "remove_attachment":
                    if index > len(draft["attachments"]):
                        raise ValueError("附件序号不存在，请先查看草稿")
                    generator = plugin.remove_submission_attachment(event, str(index) if index else "全部")
                else:
                    from astrbot.api.message_components import File, Image
                    if not any(isinstance(item, (File, Image)) for item in event.get_messages()):
                        raise ValueError("本轮没有可添加的附件，请发送或引用指定图片/文件")
                    generator = plugin.attach_to_submission(event)
                replies = [reply async for reply in generator]
            current = plugin._drafts.get(session)
            prepared = bool(current and current.get("code") != before_code)
            result = {"prepared": prepared, "submitted": False, "preview_sent": prepared,
                      "cancelled": operation == "cancel" and draft is not None and current is None,
                      "response_sent": True, "operation": operation,
                      "draft": _draft_summary(current),
                      "text": ("新预览已发送，尚未正式提交。" if prepared else
                               "草稿已取消，没有执行正式提交。" if operation == "cancel" and draft is not None and current is None else
                               "\n".join(reply if isinstance(reply, str) else reply.get_plain_text() for reply in replies)[:2000])}
            entry = _receipt(payload, result, current,
                             account_username=str(account.get("username") or "") if operation != "cancel" else None)
            try:
                for reply in replies:
                    await plugin.context.send_message(session, reply)
            except Exception:
                plugin._close_poke_window(session)
                plugin._close_attachment_window(session)
                raise
            plugin._prepared_tool_sources[cache_key] = entry
            return _current_receipt(plugin, entry, session)
