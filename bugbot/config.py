from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _parse_thread_ids(raw: str) -> frozenset[int]:
    if not raw.strip():
        return frozenset()

    try:
        return frozenset(int(value.strip()) for value in raw.split(",") if value.strip())
    except ValueError as exc:
        raise RuntimeError(
            "ALLOWED_THREAD_IDS must contain comma-separated integer IDs"
        ) from exc


def _load_google_credentials() -> dict[str, Any] | None:
    credentials_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if credentials_file:
        path = Path(credentials_file)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return json.loads(base64.b64decode(raw).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON must contain raw JSON or base64-encoded JSON"
            ) from exc


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_chat_id: int
    allowed_thread_ids: frozenset[int]
    spreadsheet_id: str
    sheet_name: str | None
    google_credentials: dict[str, Any] | None
    tag: str
    cleanup_interval_seconds: int
    fix_notification_chat_id: int | None
    fix_notification_thread_id: int | None
    image_upload_webhook_url: str | None
    image_upload_secret: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        sheet_name = os.getenv("GOOGLE_SHEET_NAME", "").strip() or None
        cleanup_interval = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "30"))
        if cleanup_interval < 10:
            raise RuntimeError("CLEANUP_INTERVAL_SECONDS must be at least 10")

        tag = os.getenv("BUG_TAG", "#new_bug").strip()
        if not tag.startswith("#"):
            raise RuntimeError("BUG_TAG must start with #")

        notification_chat = os.getenv("FIX_NOTIFICATION_CHAT_ID", "").strip()
        notification_thread = os.getenv("FIX_NOTIFICATION_THREAD_ID", "").strip()
        image_webhook = os.getenv("IMAGE_UPLOAD_WEBHOOK_URL", "").strip() or None
        image_secret = os.getenv("IMAGE_UPLOAD_SECRET", "").strip() or None
        if bool(image_webhook) != bool(image_secret):
            raise RuntimeError(
                "IMAGE_UPLOAD_WEBHOOK_URL and IMAGE_UPLOAD_SECRET must be set together"
            )

        return cls(
            telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
            allowed_chat_id=int(_required("ALLOWED_CHAT_ID")),
            allowed_thread_ids=_parse_thread_ids(
                os.getenv("ALLOWED_THREAD_IDS", "")
            ),
            spreadsheet_id=_required("GOOGLE_SPREADSHEET_ID"),
            sheet_name=sheet_name,
            google_credentials=_load_google_credentials(),
            tag=tag,
            cleanup_interval_seconds=cleanup_interval,
            fix_notification_chat_id=(
                int(notification_chat) if notification_chat else None
            ),
            fix_notification_thread_id=(
                int(notification_thread) if notification_thread else None
            ),
            image_upload_webhook_url=image_webhook,
            image_upload_secret=image_secret,
        )
