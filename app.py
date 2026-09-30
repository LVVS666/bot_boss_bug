from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from bugbot.config import Settings
from bugbot.sheets import DuplicateMessageError, GoogleSheetStore
from bugbot.utils import clean_description, telegram_message_url, topic_is_allowed


load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("telegram-bug-bot")

settings = Settings.from_env()
store = GoogleSheetStore(
    credentials_info=settings.google_credentials,
    spreadsheet_id=settings.spreadsheet_id,
    sheet_name=settings.sheet_name,
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
    """Show IDs needed to configure this chat and forum topic."""
    thread_id = message.message_thread_id
    thread_text = str(thread_id) if thread_id is not None else "нет (общий чат)"
    await message.reply(
        "ID группы: "
        f"<code>{message.chat.id}</code>\n"
        "ID темы: "
        f"<code>{thread_text}</code>"
    )


@router.message(F.photo)
async def collect_bug(message: Message) -> None:
    if not is_allowed_topic(message):
        return

    caption = message.caption or ""
    if settings.tag.lower() not in caption.lower():
        return

    description = clean_description(caption, settings.tag)
    if not description:
        await message.reply(
            f"Добавьте описание проблемы перед тегом {settings.tag}."
        )
        return

    largest_photo = message.photo[-1]
    photo_url = telegram_message_url(
        chat_id=message.chat.id,
        username=message.chat.username,
        message_id=message.message_id,
    )

    try:
        result = await asyncio.to_thread(
            store.append_bug,
            description,
            photo_url,
            message.chat.id,
            message.message_id,
            largest_photo.file_id,
        )
    except DuplicateMessageError:
        await message.reply("Эта запись уже есть в таблице.")
        return
    except Exception:
        logger.exception("Failed to append bug")
        await message.reply("Не удалось добавить запись в Google Sheets.")
        return

    builder = InlineKeyboardBuilder()
    if photo_url:
        builder.button(text="Открыть сообщение", url=photo_url)
    await message.reply(
        f"Ошибка №{result.issue_id} добавлена в реестр.",
        reply_markup=builder.as_markup() if photo_url else None,
    )


@router.message(F.text)
async def missing_photo(message: Message) -> None:
    if (
        is_allowed_topic(message)
        and settings.tag.lower() in (message.text or "").lower()
    ):
        await message.reply("Для новой ошибки приложите фотографию и описание в её подписи.")


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(settings.cleanup_interval_seconds)
        try:
            removed = await asyncio.to_thread(store.delete_checked_rows)
            if removed:
                logger.info("Deleted checked issues: %s", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Checkbox cleanup failed")


async def main() -> None:
    await asyncio.to_thread(store.initialize)
    logger.info("Using worksheet %s", store.sheet_name)
    if settings.allowed_thread_ids:
        logger.info("Allowed Telegram topic IDs: %s", sorted(settings.allowed_thread_ids))
    else:
        logger.warning(
            "ALLOWED_THREAD_IDS is empty: bug collection is disabled. "
            "Use /where in each required topic and configure their IDs."
        )

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await bot.delete_webhook(drop_pending_updates=False)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    cleanup_task = asyncio.create_task(cleanup_loop())

    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
