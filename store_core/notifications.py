"""Post-commit notifications; failures never roll back an application."""
import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .storage import Store, utc_now

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)


class Sender(Protocol):
    async def send(self, application: dict) -> None: ...


class LocalSender:
    """An audit journal deliberately excludes customer data."""
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def send(self, application: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(f"{utc_now()} application_id={application['id']}\n")


class TelegramSender:
    def __init__(self, bot: "Bot", admin_id: int) -> None:
        self.bot = bot
        self.admin_id = admin_id

    async def send(self, application: dict) -> None:
        text = (
            f"Заявка №{application['id']} — {application['status']}\n"
            f"{application['customer_name']}\nКонтакт: {application['contact']}\n"
            f"Товар: {application['product_name'] or 'Вопрос человеку'}\n"
            f"Размер: {application['size'] or '—'}\n"
            f"Вопрос: {application['original_question'] or '—'}"
        )
        for index in range(0, len(text), 1900):
            await self.bot.send_message(
                self.admin_id, text[index:index + 1900], parse_mode=None,
            )


class Notifications:
    """One process per SQLite file. Retry is explicit, delivery is at-least-once."""
    def __init__(
        self, store: Store, sender: Sender, *,
        test_mode: bool = False, fail_first: bool = False,
    ) -> None:
        if fail_first and not test_mode:
            raise ValueError("Имитация ошибки доступна только в тестовом режиме")
        self.store = store
        self.sender = sender
        self.fail_first = fail_first
        self.lock = asyncio.Lock()

    async def deliver(self, application_id: int) -> bool:
        async with self.lock:
            application = self.store.get(application_id)
            if application is None:
                raise ValueError("Заявка не найдена")
            if application["notification_state"] == "sent":
                return True
            try:
                if self.fail_first and application["notification_attempts"] == 0:
                    raise RuntimeError("Controlled test failure")
                await self.sender.send(application)
            except Exception as exc:
                # Exception messages from network clients may contain secrets.
                self.store.notification_result(application_id, type(exc).__name__)
                logger.warning(
                    "Notification failed: application_id=%s error_type=%s",
                    application_id, type(exc).__name__,
                )
                return False
            self.store.notification_result(application_id, None)
            return True
