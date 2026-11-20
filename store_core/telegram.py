"""Thin aiogram 3 adapter. The authoritative FSM lives in SQLite via Shop."""
import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update,
)

from parser_mvp import load_catalog

from .config import Settings
from .notifications import LocalSender, Notifications, TelegramSender
from .service import (
    CUSTOMER_HOME_BUTTONS, HUMAN_CONFIRMATION, ORDER_CONFIRMATION,
    REPEAT_CONFIRMATION, Reply, Shop,
)
from .storage import Store

logger = logging.getLogger(__name__)


def markup(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data)]
        for label, data in reply.buttons
    ])


def customer_reply(reply: Reply) -> Reply:
    """Hide legacy cached confirmation text without rewriting persisted events."""
    if reply.application_id is not None and reply.text not in (
        ORDER_CONFIRMATION, HUMAN_CONFIRMATION, REPEAT_CONFIRMATION,
    ):
        return Reply(
            REPEAT_CONFIRMATION, list(CUSTOMER_HOME_BUTTONS), reply.application_id,
        )
    return reply


def create_dispatcher(
    shop: Shop, notifications: Notifications, settings: Settings,
) -> Dispatcher:
    router = Router()

    async def answer(message: Message, reply: Reply, *, customer: bool = True) -> None:
        if customer:
            reply = customer_reply(reply)
        # 1900 code points also fit Telegram's UTF-16 limit with emoji.
        chunks = [
            reply.text[index:index + 1900]
            for index in range(0, len(reply.text), 1900)
        ]
        for index, chunk in enumerate(chunks):
            await message.answer(
                chunk, parse_mode=None,
                reply_markup=markup(reply) if index == len(chunks) - 1 else None,
            )

    async def deliver(reply: Reply) -> None:
        if reply.application_id is not None:
            await notifications.deliver(reply.application_id)

    @router.message(Command("admin_reload"))
    async def reload_handler(message: Message) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        try:
            reply = shop.reload_catalog(message.from_user.id, settings.catalog_path)
        except (ValueError, OSError):
            reply = Reply(
                "Каталог не загружен: проверьте файл и колонки. "
                "Прежняя версия сохранена.",
            )
        await answer(message, reply, customer=False)

    @router.message()
    async def message_handler(message: Message, event_update: Update) -> None:
        if message.from_user is None:
            return
        if message.chat.type != "private":
            await message.answer("Пожалуйста, напишите боту в личном чате, чтобы продолжить.")
            return
        if message.text is None:
            await message.answer(
                "Отправьте текст или воспользуйтесь кнопками в последнем сообщении.",
            )
            return
        reply = shop.handle(message.from_user.id, f"tg:{event_update.update_id}", message.text)
        # The durable write has already committed before either network operation.
        await deliver(reply)
        await answer(message, reply, customer=not message.text.startswith("/admin"))

    @router.callback_query()
    async def callback_handler(query: CallbackQuery, event_update: Update) -> None:
        if not isinstance(query.message, Message) or query.message.chat.type != "private":
            await query.answer("Откройте личный чат с ботом.")
            return
        # Friendly navigation buttons reuse existing commands and FSM transitions.
        command = {"ui:start": "/start", "ui:catalog": "/catalog"}.get(query.data or "")
        if command:
            reply = shop.handle(query.from_user.id, f"tg:{event_update.update_id}", command)
        else:
            reply = shop.handle(
                query.from_user.id, f"tg:{event_update.update_id}",
                callback=query.data or "",
            )
        await query.answer()
        await deliver(reply)
        await answer(
            query.message, reply,
            customer=not (query.data or "").startswith("admin:"),
        )

    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher.include_router(router)
    return dispatcher


async def run(settings: Settings) -> None:
    if not settings.token:
        raise ValueError(
            "Задайте TELEGRAM_BOT_TOKEN. Без токена используйте "
            "python -m store_core simulate",
        )
    catalog = load_catalog(settings.catalog_path)
    store = Store(settings.sqlite_path)
    async with Bot(settings.token) as bot:
        if settings.notification_backend == "telegram":
            if settings.admin_id is None:
                raise ValueError("Для уведомлений нужен Telegram user ID администратора")
            sender = TelegramSender(bot, settings.admin_id)
        else:
            sender = LocalSender(store.path.parent / "notifications.log")
        notifications = Notifications(
            store, sender, test_mode=settings.test_mode,
            fail_first=settings.fail_first,
        )
        shop = Shop(catalog, store, settings.admin_id)
        dispatcher = create_dispatcher(shop, notifications, settings)
        logger.info("Store bot started; customer data and token are excluded from logs")
        await dispatcher.start_polling(bot)


def main(settings: Settings) -> None:
    asyncio.run(run(settings))
