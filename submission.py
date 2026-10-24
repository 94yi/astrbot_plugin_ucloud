"""Compatibility exports for the shared Companion submission domain."""

from astrbot_plugin_private_companion.ucloud_submission_rules import (  # noqa: F401
    ATTACHMENT_LIMITS,
    DRAFT_TTL_SECONDS,
    MAX_ATTACHMENTS,
    MAX_CONTENT_LENGTH,
    TASK_LIST_TTL_SECONDS,
    attachment_error,
    resolve_group_id,
    submission_permission,
)


def command_tail(message: str, *, maxsplit: int = 1) -> list[str]:
    """Return slash-command arguments without losing spaces in answer text."""
    parts = str(message or "").strip().split(maxsplit=maxsplit)
    return parts[1:] if len(parts) > 1 else []
