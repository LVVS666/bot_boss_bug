from __future__ import annotations

import re


def clean_description(caption: str, tag: str) -> str:
    """Remove the routing tag while keeping the human-written description."""
    pattern = re.compile(rf"(?<!\w){re.escape(tag)}(?!\w)", re.IGNORECASE)
    without_tag = pattern.sub("", caption)
    lines = [line.strip() for line in without_tag.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def telegram_message_url(chat_id: int, username: str | None, message_id: int) -> str | None:
    """Return a link that is accessible to members of the originating chat."""
    if username:
        return f"https://t.me/{username.lstrip('@')}/{message_id}"

    chat_id_text = str(abs(chat_id))
    if chat_id_text.startswith("100") and len(chat_id_text) > 3:
        return f"https://t.me/c/{chat_id_text[3:]}/{message_id}"
    return None


def is_checked(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.upper() == "TRUE")


def topic_is_allowed(
    chat_id: int,
    thread_id: int | None,
    allowed_chat_id: int,
    allowed_thread_ids: frozenset[int],
) -> bool:
    """Accept messages only from explicitly configured forum topics."""
    return (
        chat_id == allowed_chat_id
        and thread_id is not None
        and thread_id in allowed_thread_ids
    )
