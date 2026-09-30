from __future__ import annotations

import asyncio
import html
import logging
from io import BytesIO

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, PhotoSize
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from bugbot.config import Settings
from bugbot.images import AppsScriptImageUploader, MAX_IMAGE_BYTES
from bugbot.sheets import DuplicateMessageError, FixNotification, GoogleSheetStore
from bugbot.utils import clean_description, telegram_message_url, topic_is_allowed


load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("telegram-bug-bot")

settings = Settings.from_env()
store = (
    GoogleSheetStore(
        credentials_info=settings.google_credentials,
        spreadsheet_id=settings.spreadsheet_id,
        sheet_name=settings.sheet_name,
    )
    if settings.google_credentials
    else None
)
image_uploader = (
    AppsScriptImageUploader(
        settings.image_upload_webhook_url,
        settings.image_upload_secret,
    )
    if settings.image_upload_webhook_url and settings.image_upload_secret
    else None
)
router = Router()


def is_allowed_topic(message: Message) -> bool:
    return topic_is_allowed(
        chat_id=message.chat.id,
        thread_id=message.message_thread_id,
        allowed_chat_id=settings.allowed_chat_id,
        allowed_thread_ids=settings.allowed_thread_ids,
    )


@router.message(Command("where"))
async def show_location(message: Message) -> None:
    thread_id = message.message_thread_id
    thread_text = str(thread_id) if thread_id is not None else "нет (общий чат)"
    await message.reply(
        "ID группы: "
        f"<code>{message.chat.id}</code>\n"
        "ID темы: "
        f"<code>{thread_text}</code>"
    )


@router.message(F.photo)
async def collect_photo_bug(message: Message, bot: Bot) -> None:
    if not is_allowed_topic(message):
        return

    caption = message.caption or ""
    if settings.tag.lower() not in caption.lower():
        return

    description = clean_description(caption, settings.tag)
    if not description:
        await message.reply(f"Добавьте описание проблемы перед тегом {settings.tag}.")
        return

    photo = _best_upload_photo(message.photo)
    photo_bytes: bytes | None = None
    if photo and image_uploader:
        buffer = BytesIO()
        await bot.download(photo.file_id, destination=buffer)
        candidate = buffer.getvalue()
        if len(candidate) <= MAX_IMAGE_BYTES:
            photo_bytes = candidate

    await save_bug(
        message=message,
        description=description,
        telegram_file_id=message.photo[-1].file_id,
        photo_bytes=photo_bytes,
    )


@router.message(F.text)
async def collect_text_bug(message: Message) -> None:
    if not is_allowed_topic(message):
        return

    text = message.text or ""
    if settings.tag.lower() not in text.lower():
        return

    description = clean_description(text, settings.tag)
    if not description:
        await message.reply(f"Добавьте описание проблемы перед тегом {settings.tag}.")
        return

    await save_bug(message=message, description=description)


async def save_bug(
    *,
    message: Message,
    description: str,
    telegram_file_id: str = "",
    photo_bytes: bytes | None = None,
) -> None:
    if store is None:
        await message.reply(
            "Google Sheets пока не настроен. Добавьте файл "
            "<code>service-account.json</code> и перезапустите бота."
        )
        return

    message_url = telegram_message_url(
        chat_id=message.chat.id,
        username=message.chat.username,
        message_id=message.message_id,
    )

    try:
        result = await asyncio.to_thread(
            store.append_bug,
            description,
            message_url,
            message.chat.id,
            message.message_id,
            telegram_file_id,
        )
    except DuplicateMessageError:
        await message.reply("Эта запись уже есть в таблице.")
        return
    except Exception:
        logger.exception("Failed to append bug")
        await message.reply("Не удалось добавить запись в Google Sheets.")
        return

    photo_saved = False
    if photo_bytes and image_uploader:
        try:
            await image_uploader.insert_image(
                image_bytes=photo_bytes,
                spreadsheet_id=settings.spreadsheet_id,
                sheet_name=store.sheet_name,
                row_number=result.row_number,
                image_key=f"{message.chat.id}:{message.message_id}",
            )
            photo_saved = True
        except Exception:
            logger.exception("Failed to insert photo into Google Sheets")

    builder = InlineKeyboardBuilder()
    if message_url:
        builder.button(text="Открыть сообщение", url=message_url)
    reply = f"Ошибка №{result.issue_id} добавлена в реестр."
    if telegram_file_id and not photo_saved:
        reply += " Фотография в таблицу не добавлена."
    await message.reply(
        reply,
        reply_markup=builder.as_markup() if message_url else None,
    )


def _best_upload_photo(photos: list[PhotoSize]) -> PhotoSize | None:
    for photo in reversed(photos):
        if photo.file_size is None or photo.file_size <= MAX_IMAGE_BYTES:
            return photo
    return None


async def workflow_loop(bot: Bot) -> None:
    if store is None:
        return

    while True:
        await asyncio.sleep(settings.cleanup_interval_seconds)
        try:
            if settings.fix_notification_chat_id is not None:
                pending = await asyncio.to_thread(store.get_pending_fix_notifications)
                for notification in pending:
                    await send_fix_notification(bot, notification)
                    await asyncio.to_thread(
                        store.mark_fix_notification_sent,
                        notification.source_chat_id,
                        notification.source_message_id,
                    )

            removed = await asyncio.to_thread(store.delete_fixed_rows)
            if removed:
                logger.info("Deleted fixed issues: %s", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Checkbox workflow failed")


async def send_fix_notification(bot: Bot, notification: FixNotification) -> None:
    if settings.fix_notification_chat_id is None:
        return

    thread_kwargs = {}
    if settings.fix_notification_thread_id is not None:
        thread_kwargs["message_thread_id"] = settings.fix_notification_thread_id

    if notification.telegram_file_id:
        text = _notification_text(notification, max_description_length=700)
        await bot.send_photo(
            chat_id=settings.fix_notification_chat_id,
            photo=notification.telegram_file_id,
            caption=text,
            **thread_kwargs,
        )
    else:
        text = _notification_text(notification, max_description_length=3500)
        await bot.send_message(
            chat_id=settings.fix_notification_chat_id,
            text=text,
            **thread_kwargs,
        )


def _notification_text(
    notification: FixNotification,
    *,
    max_description_length: int,
) -> str:
    raw_description = notification.description
    if len(raw_description) > max_description_length:
        raw_description = raw_description[: max_description_length - 1].rstrip() + "…"
    description = html.escape(raw_description)
    text = f"<b>Баг №{notification.issue_id} готов к проверке</b>\n\n{description}"
    if notification.message_url:
        safe_url = html.escape(notification.message_url, quote=True)
        text += f'\n\n<a href="{safe_url}">Открыть исходное сообщение</a>'
    return text


async def main() -> None:
    if store is not None:
        await asyncio.to_thread(store.initialize)
        logger.info("Using worksheet %s", store.sheet_name)
    else:
        logger.warning(
            "Google credentials are missing. Setup mode is active: /where works, "
            "but bug collection is disabled."
        )
    if settings.allowed_thread_ids:
        logger.info("Allowed Telegram topic IDs: %s", sorted(settings.allowed_thread_ids))
    else:
        logger.warning(
            "ALLOWED_THREAD_IDS is empty: bug collection is disabled. "
            "Use /where in each required topic and configure their IDs."
        )
    if settings.fix_notification_chat_id is None:
        logger.warning("FIX_NOTIFICATION_CHAT_ID is empty: fix notifications are disabled")
    if image_uploader is None:
        logger.warning("Image uploader is not configured: photo cells will stay empty")

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await bot.delete_webhook(drop_pending_updates=False)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    workflow_task = asyncio.create_task(workflow_loop(bot))

    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        workflow_task.cancel()
        await asyncio.gather(workflow_task, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
