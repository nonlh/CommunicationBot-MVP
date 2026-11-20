"""Customer-visible copy must not expose the application's internal metadata."""
import re

import pytest

from parser_mvp import load_catalog
from parser_mvp.catalog import DEFAULT_CATALOG_PATH
from store_core.service import Shop
from store_core.simulator import button
from store_core.storage import Store


@pytest.fixture
def shop(tmp_path):
    return Shop(load_catalog(DEFAULT_CATALOG_PATH), Store(tmp_path / "store.sqlite3"), 999)


def assert_customer_copy(reply):
    visible = "\n".join([reply.text, *(label for label, _ in reply.buttons)])
    for technical in (
        "/start", "/catalog", "/back", "/cancel", "application_id", "product_id",
        "operation_id", "notification_state", "Traceback", "OSError",
        "RuntimeError", "SQLite", "JSON", "Нужен ответ человека", "Обработана",
    ):
        assert technical not in visible
    assert not re.search(r"№\s*\d+|\b[0-9a-f]{32}\b|tg:\d+", visible)


def preview(shop, human=False):
    replies = []
    if human:
        reply = shop.handle(1, "question", "Из какого материала чёрная футболка?")
        replies.append(reply)
        reply = shop.handle(1, "human", callback=button(reply, "Оставить вопрос человеку"))
    else:
        reply = shop.handle(1, "start", "/start")
        replies.append(reply)
        reply = shop.handle(1, "product", callback=button(reply, "Чёрная футболка"))
        replies.append(reply)
        reply = shop.handle(1, "order", callback=button(reply, "Оформить заявку"))
        replies.append(reply)
        reply = shop.handle(1, "size", "M")
    replies.append(reply)
    replies.append(shop.handle(1, "name", "Тестовый Покупатель"))
    replies.append(shop.handle(1, "contact", "demo@example.invalid"))
    for reply in replies:
        assert_customer_copy(reply)
    return replies[-1]


@pytest.mark.parametrize("human,expected,status", [
    (
        False,
        "Спасибо! Ваша заявка зарегистрирована. "
        "Ответственный свяжется с вами, чтобы уточнить детали.",
        "Новая",
    ),
    (
        True,
        "Ваше обращение зарегистрировано и передано ответственному. "
        "Спасибо! С вами свяжутся по указанному контакту.",
        "Нужен ответ человека",
    ),
])
def test_distinct_registration_copy_keeps_internal_id_and_status(shop, human, expected, status):
    reply = preview(shop, human)
    confirmation = button(reply, "Подтвердить")
    assert not shop.store.applications()
    result = shop.handle(1, "confirm", callback=confirmation)
    assert result.text == expected
    assert_customer_copy(result)
    assert {label for label, _ in result.buttons} == {"Новое обращение", "Каталог"}
    row = shop.store.get(result.application_id)
    assert row["id"] == result.application_id
    assert row["status"] == status
    assert row["notification_state"] == "pending"
    with shop.store.connection() as db:
        session = shop.store.session(db, 1)
        assert set(session) == {"stage", "operation", "application_id", "confirmed_callback"}
        assert session["stage"] == "done"
    # Both kinds of replay remain idempotent and customer-safe.
    assert_customer_copy(shop.handle(1, "confirm", callback=confirmation))
    repeated = shop.handle(1, "repeat", callback=confirmation)
    assert repeated.text == "Ваше обращение уже зарегистрировано. Спасибо!"
    assert repeated.application_id == result.application_id
    assert_customer_copy(repeated)
    assert len(shop.store.applications()) == 1
    admin = shop.handle(999, "view", f"/admin view {result.application_id}")
    assert f"№{result.application_id}" in admin.text
    assert status in admin.text


@pytest.mark.parametrize("text", ["/cancel", "/back"])
def test_navigation_after_cancellation_uses_friendly_buttons(shop, text):
    shop.handle(1, "cancel", "/cancel")
    reply = shop.handle(1, "next", text)
    assert_customer_copy(reply)
    assert {label for label, _ in reply.buttons} == {"Новое обращение", "Каталог"}
    assert len(reply.buttons) == 2
    assert not shop.store.applications()


@pytest.mark.parametrize("text", [
    "Хочу купить", "Хочу купить синие джинсы M", "Хочу купить T99",
    "Какие условия оплаты?", "Есть серое худи S?",
])
def test_clarifications_do_not_display_parser_output_or_service_commands(shop, text):
    reply = shop.handle(1, "question", text)
    assert_customer_copy(reply)
    assert not shop.store.applications()


def test_stale_button_recovery_has_plain_language_navigation(shop):
    reply = shop.handle(1, "stale", callback="old-operation:old-button")
    assert "Кнопка устарела" in reply.text
    assert_customer_copy(reply)
    assert {label for label, _ in reply.buttons} == {"Новое обращение", "Каталог"}
