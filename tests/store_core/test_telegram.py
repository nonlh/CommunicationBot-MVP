"""Run actual aiogram dispatch with a fake transport: no Telegram servers."""
import asyncio
import json
import pytest
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from parser_mvp import load_catalog
from parser_mvp.catalog import DEFAULT_CATALOG_PATH
from store_core.config import Settings
from store_core.notifications import LocalSender, Notifications, TelegramSender
from store_core.service import Shop
from store_core.storage import Store
from store_core.telegram import create_dispatcher


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.messages = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendMessage):
            self.messages.append(method)
            return Message(
                message_id=len(self.messages), date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"), text=method.text,
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        raise AssertionError(f"Unexpected Telegram method {type(method).__name__}")

    async def stream_content(self, url, **kwargs):
        raise AssertionError("Network is forbidden")
        yield b""


def message(update_id, text=None, user_id=1, chat_type="private"):
    return Update(update_id=update_id, message=Message(
        message_id=update_id, date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="Synthetic"),
        text=text,
        entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if text and text.startswith("/") else None,
    ))


def callback(update_id, data, user_id=1):
    return Update(update_id=update_id, callback_query=CallbackQuery(
        id=str(update_id), chat_instance="test", data=data,
        from_user=User(id=user_id, is_bot=False, first_name="Synthetic"),
        message=Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=user_id, type="private")),
    ))


def callback_data(transport, label):
    return next(button.callback_data for row in transport.messages[-1].reply_markup.inline_keyboard for button in row if button.text == label)


def test_aiogram_offline_checkout_duplicate_and_admin_retry(tmp_path):
    async def scenario():
        settings = Settings(admin_id=999, test_mode=True, fail_first=True)
        store = Store(tmp_path / "bot.sqlite3")
        shop = Shop(load_catalog("tests/fixtures/store_catalog.csv"), store, 999)
        transport = FakeSession()
        bot = Bot(str(123456) + ":" + "x" * 35, session=transport)
        notifications = Notifications(store, LocalSender(tmp_path / "notifications.log"), test_mode=True, fail_first=True)
        dispatcher = create_dispatcher(shop, notifications, settings)
        async with bot:
            await dispatcher.feed_update(bot, message(1, "/start"))
            await dispatcher.feed_update(bot, callback(2, callback_data(transport, "Футболка синяя")))
            await dispatcher.feed_update(bot, callback(11, callback_data(transport, "Оформить заявку")))
            await dispatcher.feed_update(bot, callback(3, callback_data(transport, "M")))
            await dispatcher.feed_update(bot, message(4, "Тестовый Покупатель"))
            await dispatcher.feed_update(bot, message(5, "demo@example.invalid"))
            assert not store.applications()
            data = callback_data(transport, "Подтвердить")
            update = callback(6, data)
            await dispatcher.feed_update(bot, update)
            assert "заявка зарегистрирована" in transport.messages[-1].text
            assert len(store.applications()) == 1
            assert store.get(1)["notification_state"] == "failed"
            # A customer cannot invoke an administrative retry.
            await dispatcher.feed_update(bot, callback(7, "admin:retry:1"))
            assert transport.messages[-1].text == "Доступ запрещён."
            assert store.get(1)["notification_state"] == "failed"
            await dispatcher.feed_update(bot, callback(8, "admin:retry:1", user_id=999))
            assert store.get(1)["notification_state"] == "sent"
            await dispatcher.feed_update(bot, update)
            await dispatcher.feed_update(bot, callback(9, data))
            assert len(store.applications()) == 1
            assert store.get(1)["notification_attempts"] == 2
            await dispatcher.feed_update(bot, callback(10, "admin:status:1:2", user_id=999))
            assert store.get(1)["status"] == "Обработана"
        await dispatcher.storage.close()
        await dispatcher.fsm.events_isolation.close()
    asyncio.run(scenario())


def test_adapter_rejects_group_and_non_text_and_unauthorized_reload(tmp_path):
    async def scenario():
        store = Store(tmp_path / "bot.sqlite3")
        shop = Shop(load_catalog("tests/fixtures/store_catalog.csv"), store, 999)
        transport = FakeSession()
        bot = Bot(str(123456) + ":" + "x" * 35, session=transport)
        dispatcher = create_dispatcher(shop, Notifications(store, LocalSender(tmp_path / "log")), Settings(admin_id=999))
        async with bot:
            await dispatcher.feed_update(bot, message(1, "/admin_reload"))
            assert transport.messages[-1].text == "Доступ запрещён."
            await dispatcher.feed_update(bot, message(2, "/admin", chat_type="group"))
            assert "личном чате" in transport.messages[-1].text
            await dispatcher.feed_update(bot, message(3))
            assert "Отправьте текст" in transport.messages[-1].text
            assert not store.applications()
        await dispatcher.storage.close()
        await dispatcher.fsm.events_isolation.close()
    asyncio.run(scenario())


def test_telegram_notification_uses_admin_and_committed_application(tmp_path):
    async def scenario():
        transport = FakeSession()
        bot = Bot(str(123456) + ":" + "x" * 35, session=transport)
        async with bot:
            await TelegramSender(bot, 999).send({
                "id": 1, "status": "Новая", "customer_name": "Тестовый Покупатель",
                "contact": "demo@example.invalid", "product_name": "Синтетический товар",
                "size": "M", "original_question": None,
            })
        assert transport.messages[-1].chat_id == 999
        assert "Заявка №1" in transport.messages[-1].text
        assert transport.messages[-1].parse_mode is None
    asyncio.run(scenario())


def test_long_question_preview_is_sent_in_full_with_confirmation_last(tmp_path):
    async def scenario():
        store = Store(tmp_path / "bot.sqlite3")
        shop = Shop(load_catalog("tests/fixtures/store_catalog.csv"), store)
        transport = FakeSession()
        bot = Bot(str(123456) + ":" + "x" * 35, session=transport)
        dispatcher = create_dispatcher(
            shop, Notifications(store, LocalSender(tmp_path / "log")), Settings(),
        )
        question = "Почему? " + "🙂" * 3000
        async with bot:
            await dispatcher.feed_update(bot, message(1, question))
            await dispatcher.feed_update(bot, callback(
                2, callback_data(transport, "Оставить вопрос человеку"),
            ))
            await dispatcher.feed_update(bot, message(3, "Тестовый Покупатель"))
            previous_count = len(transport.messages)
            await dispatcher.feed_update(bot, message(4, "demo@example.invalid"))
            preview_messages = transport.messages[previous_count:]
            assert len(preview_messages) > 1
            assert question in "".join(method.text for method in preview_messages)
            assert all(len(method.text.encode("utf-16-le")) // 2 <= 4096 for method in preview_messages)
            assert callback_data(transport, "Подтвердить")
            assert not store.applications()
        await dispatcher.storage.close()
        await dispatcher.fsm.events_isolation.close()
    asyncio.run(scenario())


class EmployerSession(FakeSession):
    """Exercise real TelegramSender, observing commit-before-send and failures."""
    def __init__(self, store, fail_first=False):
        super().__init__()
        self.store = store
        self.fail_first = fail_first
        self.admin_attempts = 0

    async def make_request(self, bot, method, timeout=None):
        if (
            isinstance(method, SendMessage) and method.chat_id == 999
            and method.text.startswith("Заявка №")
        ):
            assert self.store.applications(), "Application must commit before sending"
            self.admin_attempts += 1
            if self.fail_first and self.admin_attempts == 1:
                raise OSError("Controlled fake Telegram transport failure")
        return await super().make_request(bot, method, timeout)


class EmployerDialog:
    def __init__(self, bot, dispatcher, transport, store):
        self.bot = bot
        self.dispatcher = dispatcher
        self.transport = transport
        self.store = store
        self.next_event = 0

    async def text(self, text, user_id=1):
        self.next_event += 1
        update = message(self.next_event, text, user_id=user_id)
        await self.dispatcher.feed_update(self.bot, update)
        return update

    async def click(self, label=None, *, data=None, user_id=1):
        self.next_event += 1
        update = callback(
            self.next_event, data or callback_data(self.transport, label), user_id,
        )
        await self.dispatcher.feed_update(self.bot, update)
        return update

    async def checkout(self):
        await self.text("/start")
        await self.click("Чёрная футболка")
        await self.click("Оформить заявку")
        await self.click("M")
        await self.text("Тестовый Покупатель")
        await self.text("demo@example.invalid")
        assert not self.store.applications()
        return await self.click("Подтвердить")


@asynccontextmanager
async def employer_dialog(tmp_path, *, fail_first=False):
    store = Store(tmp_path / "employer.sqlite3")
    settings = Settings(admin_id=999, notification_backend="telegram")
    shop = Shop(load_catalog(settings.catalog_path), store, 999)
    assert settings.catalog_path == str(DEFAULT_CATALOG_PATH)
    transport = EmployerSession(store, fail_first)
    bot = Bot(str(123456) + ":" + "x" * 35, session=transport)
    notifications = Notifications(store, TelegramSender(bot, 999))
    dispatcher = create_dispatcher(shop, notifications, settings)
    async with bot:
        try:
            yield EmployerDialog(bot, dispatcher, transport, store)
        finally:
            await dispatcher.storage.close()
            await dispatcher.fsm.events_isolation.close()


def test_required_1_available_order_saved_and_admin_notified(tmp_path):
    async def scenario():
        async with employer_dialog(tmp_path) as dialog:
            await dialog.checkout()
            rows = dialog.store.applications()
            assert len(rows) == 1
            assert rows[0]["product_id"] == "T01"
            assert rows[0]["size"] == "M"
            assert rows[0]["status"] == "Новая"
            assert rows[0]["notification_state"] == "sent"
            admin_messages = [m for m in dialog.transport.messages if m.chat_id == 999]
            assert len(admin_messages) == 1
            assert "Чёрная футболка" in admin_messages[0].text
            assert "demo@example.invalid" in admin_messages[0].text
    asyncio.run(scenario())


def test_required_2_missing_size_never_creates_standard_order(tmp_path):
    async def scenario():
        async with employer_dialog(tmp_path) as dialog:
            await dialog.text("/start")
            await dialog.click("Серое худи")
            await dialog.click("Оформить заявку")
            await dialog.text("S")
            assert "отсутствует в наличии" in dialog.transport.messages[-1].text
            await dialog.text("Подтвердить")
            assert not dialog.store.applications()
            assert dialog.transport.admin_attempts == 0
    asyncio.run(scenario())


def test_required_3_unknown_question_saved_and_forwarded_to_human(tmp_path):
    async def scenario():
        async with employer_dialog(tmp_path) as dialog:
            question = "Какой состав ткани чёрной футболки?"
            await dialog.text(question)
            assert "ответ человека" in dialog.transport.messages[-1].text
            await dialog.click("Оставить вопрос человеку")
            await dialog.text("Тестовый Покупатель")
            await dialog.text("demo@example.invalid")
            assert not dialog.store.applications()
            await dialog.click("Подтвердить")
            rows = dialog.store.applications()
            assert len(rows) == 1
            assert rows[0]["status"] == "Нужен ответ человека"
            assert rows[0]["original_question"] == question
            assert rows[0]["notification_state"] == "sent"
            admin_message = next(m for m in dialog.transport.messages if m.chat_id == 999)
            assert question in admin_message.text
    asyncio.run(scenario())


def test_required_4_replayed_event_and_button_keep_one_persistent_order(tmp_path):
    async def scenario():
        async with employer_dialog(tmp_path) as dialog:
            confirmed = await dialog.checkout()
            before = dialog.store.applications()
            await dialog.dispatcher.feed_update(dialog.bot, confirmed)
            await dialog.click(data=confirmed.callback_query.data)
            assert dialog.store.applications() == before
            reopened = Store(dialog.store.path)
            assert reopened.applications() == before
            assert dialog.transport.admin_attempts == 1
            # A new independent checkout by the same buyer is allowed.
            await dialog.text("/start")
            await dialog.click("Серое худи")
            await dialog.click("Оформить заявку")
            await dialog.click("L")
            await dialog.text("Тестовый Покупатель")
            await dialog.text("demo@example.invalid")
            await dialog.click("Подтвердить")
            assert len(reopened.applications()) == 2
            assert reopened.applications()[0] == before[0]
    asyncio.run(scenario())


def test_required_5_notification_failure_retry_preserves_single_order(tmp_path):
    async def scenario():
        async with employer_dialog(tmp_path, fail_first=True) as dialog:
            await dialog.checkout()
            row = dialog.store.applications()[0]
            assert row["notification_state"] == "failed"
            assert row["notification_attempts"] == 1
            assert row["notification_error"] == "OSError"
            reopened = Store(dialog.store.path)
            assert reopened.get(row["id"]) == row
            await dialog.click(data=f"admin:retry:{row['id']}", user_id=999)
            rows = reopened.applications()
            assert len(rows) == 1
            assert rows[0]["id"] == row["id"]
            assert rows[0]["operation_id"] == row["operation_id"]
            assert rows[0]["notification_state"] == "sent"
            assert rows[0]["notification_attempts"] == 2
            assert rows[0]["notification_error"] is None
            assert dialog.transport.admin_attempts == 2
    asyncio.run(scenario())


def test_customer_confirmation_hides_id_but_admin_notification_keeps_it(tmp_path):
    async def scenario():
        async with employer_dialog(tmp_path) as dialog:
            await dialog.checkout()
            customer = dialog.transport.messages[-1]
            assert customer.text == (
                "Спасибо! Ваша заявка зарегистрирована. "
                "Ответственный свяжется с вами, чтобы уточнить детали."
            )
            assert "№" not in customer.text
            assert "/start" not in customer.text
            row = dialog.store.applications()[0]
            admin = next(m for m in dialog.transport.messages if m.chat_id == 999)
            assert f"Заявка №{row['id']}" in admin.text
            assert "Новая" in admin.text
            await dialog.click("Новое обращение")
            assert "Выберите товар" in dialog.transport.messages[-1].text
            with dialog.store.connection() as db:
                session = dialog.store.session(db, 1)
                assert session["stage"] == "product"
                assert session["operation"] != row["operation_id"]
            assert len(dialog.store.applications()) == 1
    asyncio.run(scenario())


def test_cancelled_customer_can_open_catalog_with_button(tmp_path):
    async def scenario():
        async with employer_dialog(tmp_path) as dialog:
            await dialog.text("/cancel")
            assert "/start" not in dialog.transport.messages[-1].text
            await dialog.click("Каталог")
            assert "Выберите товар" in dialog.transport.messages[-1].text
            assert callback_data(dialog.transport, "Чёрная футболка")
            assert not dialog.store.applications()
    asyncio.run(scenario())


def test_failed_notification_does_not_expose_error_or_status_to_customer(tmp_path):
    async def scenario():
        async with employer_dialog(tmp_path, fail_first=True) as dialog:
            await dialog.checkout()
            visible = dialog.transport.messages[-1].text
            assert "заявка зарегистрирована" in visible
            for internal in ("OSError", "failed", "notification", "№", "JSON", "Новая"):
                assert internal not in visible
            row = dialog.store.applications()[0]
            assert row["notification_state"] == "failed"
            assert row["notification_error"] == "OSError"
            await dialog.text(f"/admin view {row['id']}", user_id=999)
            assert "OSError" in dialog.transport.messages[-1].text
            assert f"№{row['id']}" in dialog.transport.messages[-1].text
            # Presentation filtering must not replace an administrative retry reply.
            await dialog.click(data=f"admin:retry:{row['id']}", user_id=999)
            assert dialog.transport.messages[-1].text == "Повтор уведомления запрошен."
    asyncio.run(scenario())


def test_legacy_cached_confirmation_is_hidden_without_rewriting_database(tmp_path):
    async def scenario():
        async with employer_dialog(tmp_path) as dialog:
            confirmed = await dialog.checkout()
            event_id = f"tg:{confirmed.update_id}"
            with dialog.store.connection() as db:
                record = db.execute("SELECT response FROM events WHERE event_id=?", (event_id,)).fetchone()
                payload = json.loads(record["response"])
                payload["text"] = f"Заявка №{payload['application_id']} сохранена. /start — новое обращение."
                legacy = json.dumps(payload, ensure_ascii=False)
                db.execute("UPDATE events SET response=? WHERE event_id=?", (legacy, event_id))
            await dialog.dispatcher.feed_update(dialog.bot, confirmed)
            visible = dialog.transport.messages[-1].text
            assert visible == "Ваше обращение уже зарегистрировано. Спасибо!"
            assert "№" not in visible
            with dialog.store.connection() as db:
                assert db.execute("SELECT response FROM events WHERE event_id=?", (event_id,)).fetchone()[0] == legacy
            assert len(dialog.store.applications()) == 1
            assert dialog.transport.admin_attempts == 1
    asyncio.run(scenario())


def test_information_messages_require_order_button_before_size_selection(tmp_path):
    async def scenario():
        async with employer_dialog(tmp_path) as dialog:
            for text in (
                "Здравствуйте! Сколько стоит чёрная футболка?",
                "Какие размеры чёрной футболки?", "Есть чёрная футболка M?",
            ):
                await dialog.text(text)
                view = dialog.transport.messages[-1]
                assert "Выберите размер" not in view.text
                assert "Введите имя" not in view.text
                assert callback_data(dialog.transport, "Оформить заявку")
                assert not dialog.store.applications()
                with dialog.store.connection() as db:
                    assert dialog.store.session(db, 1)["stage"] == "product"
            await dialog.click("Оформить заявку")
            assert callback_data(dialog.transport, "M")
            with dialog.store.connection() as db:
                assert dialog.store.session(db, 1)["stage"] == "size"
    asyncio.run(scenario())


def test_greeting_after_completion_is_not_a_repeat_or_notification_retry(tmp_path):
    async def scenario():
        async with employer_dialog(tmp_path, fail_first=True) as dialog:
            confirmed = await dialog.checkout()
            await dialog.text("Здравствуйте")
            view = dialog.transport.messages[-1]
            assert "Здравствуйте" in view.text
            assert "зарегистрировано" not in view.text
            assert dialog.transport.admin_attempts == 1
            assert len(dialog.store.applications()) == 1
            await dialog.click(data=confirmed.callback_query.data)
            assert "уже зарегистрировано" in dialog.transport.messages[-1].text
            assert dialog.transport.admin_attempts == 2
            assert len(dialog.store.applications()) == 1
    asyncio.run(scenario())


def test_size_stage_distinguishes_greeting_unknown_text_and_missing_size(tmp_path):
    async def scenario():
        async with employer_dialog(tmp_path) as dialog:
            await dialog.text("Хочу купить чёрную футболку")
            for text in ("Здравствуйте", "не знаю"):
                await dialog.text(text)
                assert "отсутствует" not in dialog.transport.messages[-1].text
                with dialog.store.connection() as db:
                    assert dialog.store.session(db, 1)["stage"] == "size"
                assert callback_data(dialog.transport, "M")
            await dialog.text("размер XL")
            assert "отсутствует в наличии" in dialog.transport.messages[-1].text
            await dialog.click("M")
            await dialog.text("Здравствуйте")
            with dialog.store.connection() as db:
                current = dialog.store.session(db, 1)
                assert current["stage"] == "name"
                assert "name" not in current
            assert not dialog.store.applications()
    asyncio.run(scenario())


@pytest.mark.parametrize("text,expected,missing,possible", [
    ("здравствуйте а сколько стоит серая футболка?", "Такого товара нет в каталоге.", True, False),
    ("Сколько стоит чёрная футболка?", "2490 RUB", False, False),
    ("Сколько стоит серое худи?", "5990 RUB", False, False),
    ("Сколько стоит синие джинсы?", "6490 RUB", False, False),
    ("Сколько стоит чорная футблка?", "Подтвердите товар", False, True),
    ("Сколько стоит красная футболка?", "Такого товара нет в каталоге.", True, False),
    ("Сколько стоит доставка футболки?", "ответ человека", False, False),
])
def test_required_type_color_cases_in_actual_telegram_reply(tmp_path, text, expected, missing, possible):
    async def scenario():
        async with employer_dialog(tmp_path) as dialog:
            await dialog.text(text)
            view = dialog.transport.messages[-1]
            assert expected in view.text
            if missing:
                assert view.text == "Такого товара нет в каталоге."
                labels = [b.text for row in view.reply_markup.inline_keyboard for b in row]
                assert labels == ["Каталог"]
                assert "человека" not in view.text
                with dialog.store.connection() as db:
                    assert dialog.store.session(db, 1)["stage"] == "product"
            if possible:
                assert "2490" not in view.text
                assert callback_data(dialog.transport, "Чёрная футболка")
                await dialog.click("Чёрная футболка")
                assert "2490 RUB" in dialog.transport.messages[-1].text
                assert callback_data(dialog.transport, "Оформить заявку")
            if "доставка" in text.casefold():
                assert "Цена:" not in view.text
            assert not dialog.store.applications()
            assert dialog.transport.admin_attempts == 0
    asyncio.run(scenario())


def test_missing_type_color_does_not_reuse_previously_selected_product(tmp_path):
    async def scenario():
        async with employer_dialog(tmp_path) as dialog:
            await dialog.text("Сколько стоит чёрная футболка?")
            await dialog.text("здравствуйте а сколько стоит серая футболка?")
            assert dialog.transport.messages[-1].text == "Такого товара нет в каталоге."
            assert not dialog.store.applications()
    asyncio.run(scenario())


@pytest.mark.parametrize("text,expected,stage", [
    ("Здравствуйте, а есть серая футболка?", "Такого товара нет в каталоге.", "product"),
    ("Есть чёрное худи?", "Такого товара нет в каталоге.", "product"),
    ("Есть чёрные джинсы?", "Такого товара нет в каталоге.", "product"),
    ("Сколько стоит синяя футболка?", "Такого товара нет в каталоге.", "product"),
    ("Сколько стоит чёрная футболка?", "2490 RUB", "product"),
    ("Есть чорная футблка?", "Подтвердите товар", "product"),
    ("Можно заказать синие джинсы?", "Сейчас товара нет в наличии.", "product"),
    ("Есть синие джинсы M?", "Сейчас товара нет в наличии.", "product"),
    ("Сколько стоят синие джинсы?", "6490 RUB", "product"),
    ("Когда появятся синие джинсы?", "ответ человека", "human_offer"),
    ("Хочу чёрную футболку XL", "размер отсутствует в наличии", "size"),
    ("Хочу серое худи M", "Введите имя покупателя", "name"),
    ("Сколько стоит доставка худи?", "ответ человека", "human_offer"),
])
def test_matching_and_stock_in_telegram_adapter(tmp_path, text, expected, stage):
    """Real dispatcher and serialization, with an isolated fake Telegram transport."""
    async def scenario():
        async with employer_dialog(tmp_path) as dialog:
            await dialog.text(text)
            view = dialog.transport.messages[-1]
            assert expected in view.text
            with dialog.store.connection() as db:
                assert dialog.store.session(db, 1)["stage"] == stage
            labels = [b.text for row in view.reply_markup.inline_keyboard for b in row]
            if "нет в каталоге" in view.text or "Сейчас товара нет" in view.text:
                assert "Каталог" in labels
                assert "человек" not in view.text
                assert not {"Оформить заявку", "Оставить вопрос человеку", "M", "Подтвердить"} & set(labels)
            if "Сейчас товара нет" in view.text:
                assert view.text == "Синие джинсы\nЦена: 6490 RUB\nСейчас товара нет в наличии."
            assert not dialog.store.applications()
            assert dialog.transport.admin_attempts == 0
    asyncio.run(scenario())
