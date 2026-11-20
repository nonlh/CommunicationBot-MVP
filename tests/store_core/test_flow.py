import asyncio
import sqlite3
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import pytest

from parser_mvp import Catalog, Product, load_catalog
from store_core.config import Settings
from store_core.notifications import LocalSender, Notifications
from store_core.service import Shop
from store_core.simulator import button, simulate
from store_core.storage import Store


@pytest.fixture
def shop(tmp_path):
    return Shop(load_catalog("tests/fixtures/store_catalog.csv"), Store(tmp_path / "store.sqlite3"), admin_id=999)


def checkout(shop, prefix="one", user=1):
    reply = shop.handle(user, prefix + "start", "/start")
    reply = shop.handle(user, prefix + "product", callback=button(reply, "Футболка синяя"))
    reply = shop.handle(user, prefix + "order", callback=button(reply, "Оформить заявку"))
    reply = shop.handle(user, prefix + "size", "M")
    shop.handle(user, prefix + "name", "Тестовый Покупатель")
    return shop.handle(user, prefix + "contact", "demo@example.invalid")


def confirm(shop, prefix="one", user=1):
    reply = checkout(shop, prefix, user)
    return shop.handle(user, prefix + "confirm", callback=button(reply, "Подтвердить"))


def test_available_product_creates_only_after_confirmation(shop):
    preview = checkout(shop)
    assert not shop.store.applications()
    result = shop.handle(1, "confirm", callback=button(preview, "Подтвердить"))
    row = shop.store.get(result.application_id)
    assert row["product_id"] == "DEMO-BLUE"
    assert row["size"] == "M"
    assert row["status"] == "Новая"
    assert row["notification_state"] == "pending"


def test_wrong_size_and_out_of_order_messages(shop):
    reply = shop.handle(1, "start", "/start")
    reply = shop.handle(1, "product", callback=button(reply, "Футболка синяя"))
    shop.handle(1, "order", callback=button(reply, "Оформить заявку"))
    reply = shop.handle(1, "size", "XL")
    assert "не указан" in reply.text
    shop.handle(1, "wrong-name", "Тестовый Покупатель")
    shop.handle(1, "wrong-confirm", "Подтвердить")
    assert not shop.store.applications()
    with shop.store.connection() as db:
        assert shop.store.session(db, 1)["stage"] == "size"


def test_human_question_saved_verbatim_after_confirmation(shop):
    question = "Из какого материала футболка?"
    reply = shop.handle(1, "question", question)
    assert "ответ человека" in reply.text
    assert not shop.store.applications()
    shop.handle(1, "human", callback=button(reply, "Оставить вопрос человеку"))
    shop.handle(1, "name", "Тестовый Покупатель")
    reply = shop.handle(1, "contact", "demo@example.invalid")
    result = shop.handle(1, "confirm", callback=button(reply, "Подтвердить"))
    row = shop.store.get(result.application_id)
    assert row["original_question"] == question
    assert row["status"] == "Нужен ответ человека"
    assert row["product_id"] is None


def test_event_and_button_replay_and_independent_application(shop):
    preview = checkout(shop)
    data = button(preview, "Подтвердить")
    first = shop.handle(1, "confirm", callback=data)
    assert asdict(shop.handle(1, "confirm", callback=data)) == asdict(first)
    assert shop.handle(1, "confirm-again", callback=data).application_id == first.application_id
    second = confirm(shop, "two")
    assert second.application_id != first.application_id
    assert len(shop.store.applications()) == 2


def test_concurrent_confirmations_have_one_application(shop):
    preview = checkout(shop)
    data = button(preview, "Подтвердить")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: shop.handle(1, f"confirm{i}", callback=data), range(8)))
    assert len({reply.application_id for reply in results}) == 1
    assert len(shop.store.applications()) == 1


def test_sql_unique_operation_and_confirmation_event_constraints(shop):
    first = confirm(shop)
    original = shop.store.get(first.application_id)
    with shop.store.transaction() as db:
        session = {"operation": original["operation_id"], "name": "Тест", "contact": "demo@example.invalid"}
        assert shop.store.create_application(db, 1, "other-event", session) == first.application_id
    with pytest.raises(sqlite3.IntegrityError):
        with shop.store.transaction() as db:
            session["operation"] = "new-operation"
            shop.store.create_application(db, 1, original["confirmation_event"], session)
    assert len(shop.store.applications()) == 1


def test_reopen_preserves_application_and_mid_checkout_session(shop):
    preview = checkout(shop)
    restarted = Shop(shop.catalog, Store(shop.store.path), admin_id=999)
    result = restarted.handle(1, "confirm", callback=button(preview, "Подтвердить"))
    reopened = Store(shop.store.path)
    assert reopened.get(result.application_id)["contact"] == "demo@example.invalid"
    assert reopened.applications()[0]["id"] == result.application_id


def test_back_cancel_and_stale_confirm(shop):
    preview = checkout(shop)
    stale = button(preview, "Подтвердить")
    reply = shop.handle(1, "back", "/back")
    assert "контакт" in reply.text
    assert "устарела" in shop.handle(1, "stale", callback=stale).text
    shop.handle(1, "cancel", "/cancel")
    shop.handle(1, "after-cancel", callback=stale)
    assert not shop.store.applications()


def test_fuzzy_candidate_needs_explicit_selection(shop):
    reply = shop.handle(1, "fuzzy", "Хочу купить футбалка синяя M")
    assert "Подтвердите товар" in reply.text
    assert not shop.store.applications()
    with shop.store.connection() as db:
        assert "product_id" not in shop.store.session(db, 1)
    reply = shop.handle(1, "choose", callback=button(reply, "Футболка синяя"))
    assert "Размеры по каталогу" in reply.text


def test_catalog_change_before_confirmation_revalidates_size(shop, tmp_path):
    preview = checkout(shop)
    path = tmp_path / "changed.csv"
    path.write_text("id;name;sizes\nDEMO-BLUE;Футболка синяя;XL\n", encoding="utf-8")
    shop.reload_catalog(999, str(path))
    reply = shop.handle(1, "confirm", callback=button(preview, "Подтвердить"))
    assert "Каталог изменился" in reply.text
    assert not shop.store.applications()


@pytest.mark.parametrize("command", ["/admin", "/admin list", "/admin stats", "/admin status 1 2", "/admin retry 1", "/admin pending", "/admin catalog"])
def test_admin_commands_forbidden_to_customer(shop, command):
    assert shop.handle(1, command, command).text == "Доступ запрещён."


def test_admin_callbacks_reload_and_status_permissions(shop, tmp_path):
    result = confirm(shop)
    assert shop.handle(1, "deny", callback=f"admin:status:{result.application_id}:2").text == "Доступ запрещён."
    assert shop.reload_catalog(1, "missing.csv").text == "Доступ запрещён."
    shop.handle(999, "status", callback=f"admin:status:{result.application_id}:2")
    assert shop.store.get(result.application_id)["status"] == "Обработана"
    assert "Всего заявок: 1" in shop.handle(999, "stats", "/admin stats").text
    old = shop.catalog
    with pytest.raises(ValueError):
        shop.reload_catalog(999, "missing.csv")
    assert shop.catalog is old


def test_notification_failure_retry_persisted_and_no_second_application(shop, tmp_path):
    async def scenario():
        result = confirm(shop)
        journal = tmp_path / "notifications.log"
        notifier = Notifications(shop.store, LocalSender(journal), test_mode=True, fail_first=True)
        assert not await notifier.deliver(result.application_id)
        row = Store(shop.store.path).get(result.application_id)
        assert row["notification_state"] == "failed"
        assert row["notification_error"] == "RuntimeError"
        # New service instance after restart still sees the previous attempt.
        notifier = Notifications(Store(shop.store.path), LocalSender(journal), test_mode=True, fail_first=True)
        assert await notifier.deliver(result.application_id)
        assert await notifier.deliver(result.application_id)
        row = shop.store.get(result.application_id)
        assert row["notification_attempts"] == 2
        assert row["notification_state"] == "sent"
        assert len(shop.store.applications()) == 1
        assert len(journal.read_text().splitlines()) == 1
        assert "demo@example.invalid" not in journal.read_text()
    asyncio.run(scenario())


def test_sender_failure_keeps_application_without_leaking_exception(shop):
    class BrokenSender:
        async def send(self, application):
            assert shop.store.get(application["id"]) is not None
            raise OSError("secret-must-not-be-stored")

    async def scenario():
        result = confirm(shop)
        notifier = Notifications(shop.store, BrokenSender())
        assert not await notifier.deliver(result.application_id)
        assert shop.store.get(result.application_id)["notification_error"] == "OSError"
    asyncio.run(scenario())


def test_test_failure_requires_explicit_test_mode(shop):
    with pytest.raises(ValueError):
        Settings(fail_first=True)
    with pytest.raises(ValueError):
        Notifications(shop.store, LocalSender("unused.log"), fail_first=True)


def test_missing_price_and_real_stock_can_be_escalated(shop):
    reply = shop.handle(1, "missing-price", "Какая цена футболка белая?")
    assert "Цена не указана" in reply.text
    assert button(reply, "Оставить вопрос человеку")
    reply = shop.handle(2, "stock", "Есть футболка синяя M?")
    assert "не содержит складских остатков" in reply.text
    shop.handle(2, "stock-human", callback=button(reply, "Оставить вопрос человеку"))
    with shop.store.connection() as db:
        session = shop.store.session(db, 2)
        assert session["stage"] == "name"
        assert session["human"]
        assert session["question"] == "Есть футболка синяя M?"


def test_event_replay_cannot_be_claimed_by_another_customer(shop):
    reply = shop.handle(1, "shared-event", "/start")
    denied = shop.handle(2, "shared-event", "/start")
    assert "Не удалось открыть это сообщение" in denied.text
    assert not denied.buttons
    assert reply.buttons


def test_pagination_and_stale_product_buttons(shop):
    shop.catalog = Catalog(tuple(
        Product(str(index), f"Товар {index}", sizes=("M",))
        for index in range(12)
    ))
    reply = shop.handle(1, "start", "/start")
    stale = button(reply, "Товар 0")
    reply = shop.handle(1, "next", callback=button(reply, "Следующие товары"))
    assert button(reply, "Товар 11")
    assert "устарела" in shop.handle(1, "old", callback=stale).text
    reply = shop.handle(1, "product", callback=button(reply, "Товар 11"))
    assert "Товар 11" in reply.text
    assert len(reply.buttons) < 20


def test_no_sizes_does_not_create_false_order(shop):
    shop.catalog = Catalog((Product("A", "Тестовый товар"),))
    reply = shop.handle(1, "start", "/start")
    reply = shop.handle(1, "product", callback=button(reply, "Тестовый товар"))
    assert "не указаны" in reply.text
    assert button(reply, "Оставить вопрос человеку")
    assert not shop.store.applications()


@pytest.mark.parametrize("contact", ["bad-contact", "123", "x@", "@tiny", "/startover"])
def test_bad_contact_does_not_advance_to_confirmation(shop, contact):
    reply = shop.handle(1, "start", "/start")
    reply = shop.handle(1, "product", callback=button(reply, "Футболка синяя"))
    shop.handle(1, "order", callback=button(reply, "Оформить заявку"))
    shop.handle(1, "size", "M")
    shop.handle(1, "name", "Тестовый Покупатель")
    reply = shop.handle(1, "contact", contact)
    assert not any(label == "Подтвердить" for label, _ in reply.buttons)
    assert not shop.store.applications()


def test_end_to_end_simulator_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    result = asyncio.run(simulate("tests/fixtures/store_catalog.csv", str(tmp_path / "simulation.sqlite3")))
    assert result["application"]["id"] == result["reply"]["application_id"]
    assert all(result["checks"].values())


def test_bot_cli_without_token_reports_error_without_network():
    env = dict(os.environ)
    for name in (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_ADMIN_ID", "NOTIFICATION_BACKEND",
        "STORE_TEST_MODE", "NOTIFICATION_FAIL_FIRST",
    ):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-m", "store_core", "bot"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=15,
    )
    assert result.returncode == 2
    assert "Запуск не выполнен" in result.stderr
    assert "store_core simulate" in result.stderr
    assert "Traceback" not in result.stderr
