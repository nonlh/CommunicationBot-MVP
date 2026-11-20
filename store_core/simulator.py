"""Repeatable offline end-to-end demonstration with synthetic customer data."""
import asyncio
from dataclasses import asdict
from uuid import uuid4

from parser_mvp import load_catalog

from .notifications import LocalSender, Notifications
from .service import Reply, Shop
from .storage import Store


def button(reply: Reply, label: str) -> str:
    return next(data for title, data in reply.buttons if title == label)


async def simulate(catalog_path: str, sqlite_path: str) -> dict:
    store = Store(sqlite_path)
    shop = Shop(load_catalog(catalog_path), store, admin_id=999)
    user_id = 100
    prefix = "simulation:" + uuid4().hex
    reply = shop.handle(user_id, prefix + ":start", "/start")
    products = shop.catalog.visible_products
    product = next((p for p in products if p.sizes), None)
    if product is None:
        raise ValueError("Для симуляции нужен хотя бы один активный товар с размером")
    page = products.index(product) // 10
    for index in range(page):
        reply = shop.handle(
            user_id, prefix + f":page{index}",
            callback=button(reply, "Следующие товары"),
        )
    reply = shop.handle(
        user_id, prefix + ":product", callback=button(reply, product.name[:100]),
    )
    reply = shop.handle(
        user_id, prefix + ":order", callback=button(reply, "Оформить заявку"),
    )
    reply = shop.handle(user_id, prefix + ":size", product.sizes[0])
    shop.handle(user_id, prefix + ":name", "Тестовый Покупатель")
    reply = shop.handle(user_id, prefix + ":contact", "demo@example.invalid")
    assert not any(
        row["confirmation_event"] == prefix + ":confirm"
        for row in store.applications()
    )
    confirmation = button(reply, "Подтвердить")
    confirmed = shop.handle(user_id, prefix + ":confirm", callback=confirmation)
    duplicate = shop.handle(user_id, prefix + ":confirm", callback=confirmation)
    repeated_button = shop.handle(user_id, prefix + ":confirm-again", callback=confirmation)
    application_id = confirmed.application_id
    assert application_id == duplicate.application_id == repeated_button.application_id
    notifications = Notifications(
        store, LocalSender(store.path.parent / "notifications.log"),
        test_mode=True, fail_first=True,
    )
    assert not await notifications.deliver(application_id)
    assert store.get(application_id)["notification_state"] == "failed"
    assert await notifications.deliver(application_id)
    reopened = Store(sqlite_path)
    application = reopened.get(application_id)
    assert application["notification_state"] == "sent"
    assert sum(
        row["operation_id"] == application["operation_id"]
        for row in reopened.applications()
    ) == 1
    return {
        "reply": asdict(confirmed), "application": application,
        "checks": {
            "event_replay": True, "button_replay": True,
            "notification_failure_and_retry": True,
            "persisted_after_reopen": True,
        },
        "sqlite_path": str(store.path.resolve()),
    }


def run(catalog_path: str, sqlite_path: str) -> dict:
    return asyncio.run(simulate(catalog_path, sqlite_path))
