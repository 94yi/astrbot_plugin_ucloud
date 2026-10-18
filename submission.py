"""Student-page-compatible homework submission rules and draft helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

MAX_CONTENT_LENGTH = 2000
MAX_ATTACHMENTS = 10
DRAFT_TTL_SECONDS = 30 * 60
TASK_LIST_TTL_SECONDS = 10 * 60

# Limits copied from the current UCloud student assignment upload component.
ATTACHMENT_LIMITS: dict[str, int] = {
    "mp3": 100 * 1024 * 1024,
    "m4a": 100 * 1024 * 1024,
    "mp4": 200 * 1024 * 1024,
    "jpg": 20 * 1024 * 1024,
    "jpeg": 20 * 1024 * 1024,
    "gif": 20 * 1024 * 1024,
    "png": 20 * 1024 * 1024,
    "txt": 100 * 1024 * 1024,
    "doc": 100 * 1024 * 1024,
    "docx": 100 * 1024 * 1024,
    "pdf": 100 * 1024 * 1024,
    "xls": 100 * 1024 * 1024,
    "xlsx": 100 * 1024 * 1024,
    "ppt": 300 * 1024 * 1024,
    "pptx": 300 * 1024 * 1024,
    "rar": 3 * 1024 * 1024 * 1024,
    "zip": 3 * 1024 * 1024 * 1024,
}


def command_tail(message: str, *, maxsplit: int = 1) -> list[str]:
    """Return arguments after an AstrBot slash command without losing spaces."""
    parts = str(message or "").strip().split(maxsplit=maxsplit)
    return parts[1:] if len(parts) > 1 else []


def submission_permission(state: dict[str, Any]) -> tuple[bool, str]:
    """Mirror ``MyAssignment.showPage`` from the current UCloud student SPA."""
    try:
        phase = int(state.get("status", state.get("evaluationStatus", -1)))
        assignment_status = int(state.get("assignmentStatus", -1))
        overtime_flag = int(state.get("isOvertimeCommit", 1))
    except (TypeError, ValueError):
        return False, "教学云返回了无法识别的作业状态"

    if phase == 1:
        return False, "当前作业尚未到开始时间，不可提交"
    if assignment_status == 1:
        return False, "当前作业已批改，只能查看"
    if phase == 2 and assignment_status in {0, 2, 99}:
        return True, "进行中"
    if phase == 3 and assignment_status in {2, 99} and overtime_flag == 0:
        return True, "已截止，但教师允许逾期提交"
    if phase == 3 and assignment_status == 0:
        return False, "当前作业已截止且已提交，只能查看"
    if phase == 3 and overtime_flag == 1:
        return False, "当前作业已截止，不可提交"
    return False, "当前状态下学生端不允许提交"


def attachment_error(path: str, filename: str) -> str:
    """Validate one local attachment against the student page's rules."""
    file_path = Path(path)
    if not file_path.is_file():
        return "附件文件不可用"
    size = file_path.stat().st_size
    if size <= 0:
        return "附件不能为空"
    suffix = Path(filename).suffix.lower().lstrip(".")
    limit = ATTACHMENT_LIMITS.get(suffix)
    if limit is None:
        return f"学生端不支持 .{suffix or '未知'} 格式"
    if size >= limit:
        return f"附件超过学生端对 .{suffix} 文件的大小限制"
    return ""


def resolve_group_id(*sources: dict[str, Any]) -> str:
    """Resolve the student subgroup identifier exposed by list/detail APIs."""
    for source in sources:
        for key in ("studentGroupId", "subGroupId", "teamId", "groupId"):
            value = source.get(key)
            if value not in (None, "", 0, "0"):
                return str(value)
    return ""
