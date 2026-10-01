from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

from bugbot.config import Settings
from bugbot.sheets import (
    CompetenceNotification,
    DuplicateMessageError,
    FixNotification,
    GoogleSheetStore,
)
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
async def collect_photo_bug(message: Message) -> None:
    if not is_allowed_topic(message):
        return

    caption = message.caption or ""
    if settings.tag.lower() not in caption.lower():
        return

    description = clean_description(caption, settings.tag)
    if not description:
        await message.reply(f"Добавьте описание проблемы перед тегом {settings.tag}.")
        return

    await save_bug(message=message, description=description)


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
        )
    except DuplicateMessageError:
        await message.reply("Эта запись уже есть в таблице.")
        return
    except Exception:
        logger.exception("Failed to append bug")
        await message.reply("Не удалось добавить запись в Google Sheets.")
        return

    await message.reply(
        _bug_message(
            title=f"Баг №{result.issue_id} заведен",
            description=description,
            row_url=result.row_url,
        )
    )


async def workflow_loop(bot: Bot) -> None:
    if store is None:
        return

    while True:
        await asyncio.sleep(settings.cleanup_interval_seconds)
        try:
            pending_competence = await asyncio.to_thread(
                store.get_pending_competence_notifications
            )
            for notification in pending_competence:
                try:
                    sent = await send_competence_notification(bot, notification)
                    if sent:
                        await asyncio.to_thread(
                            store.mark_competence_notification_sent,
                            notification.source_chat_id,
                            notification.source_message_id,
                        )
                except Exception:
                    logger.exception(
                        "Failed to send competence notification for bug %s",
                        notification.issue_id,
                    )

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


async def send_competence_notification(
    bot: Bot,
    notification: CompetenceNotification,
) -> bool:
    competence = notification.competence.casefold()
    developer = settings.dev_front if competence == "фронт" else settings.dev_back
    if not developer:
        variable = "DEV_FRONT" if competence == "фронт" else "DEV_BACK"
        logger.warning(
            "%s is empty: notification for bug %s remains pending",
            variable,
            notification.issue_id,
        )
        return False

    await bot.send_message(
        chat_id=settings.allowed_chat_id,
        message_thread_id=settings.dev_thread_id,
        text=_bug_message(
            title=f"Баг №{notification.issue_id} заведен",
            description=notification.description,
            row_url=notification.row_url,
            mention=developer,
        ),
    )
    return True


async def send_fix_notification(bot: Bot, notification: FixNotification) -> None:
    if settings.fix_notification_chat_id is None:
        return

    thread_kwargs = {}
    if settings.fix_notification_thread_id is not None:
        thread_kwargs["message_thread_id"] = settings.fix_notification_thread_id

    await bot.send_message(
        chat_id=settings.fix_notification_chat_id,
        text=_bug_message(
            title=f"Баг №{notification.issue_id} исправлен и готов к проверке",
            description=notification.description,
            row_url=notification.row_url,
        ),
        **thread_kwargs,
    )


def _bug_message(
    *,
    title: str,
    description: str,
    row_url: str,
    mention: str | None = None,
) -> str:
    if len(description) > 3500:
        description = description[:3499].rstrip() + "…"
    parts = [f"<b>{html.escape(title)}</b>"]
    if mention:
        parts.append(_developer_mention(mention))
    parts.append(html.escape(description))
    safe_url = html.escape(row_url, quote=True)
    parts.append(f'<a href="{safe_url}">Ссылка</a>')
    return "\n\n".join(parts)


def _developer_mention(value: str) -> str:
    value = value.strip()
    if value.isdigit():
        return f'<a href="tg://user?id={value}">Разработчик</a>'
    return html.escape(value)


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
    if settings.dev_front is None:
        logger.warning("DEV_FRONT is empty: frontend notifications are pending")
    if settings.dev_back is None:
        logger.warning("DEV_BACK is empty: backend notifications are pending")
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
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
