"""Administrative formatting preserves stored data and existing actions."""
import pytest

from parser_mvp import load_catalog
from parser_mvp.catalog import DEFAULT_CATALOG_PATH
from store_core.service import Shop
from store_core.simulator import button
from store_core.storage import Store


@pytest.fixture
def order(tmp_path):
    shop = Shop(load_catalog(DEFAULT_CATALOG_PATH), Store(tmp_path / "admin.sqlite3"), 999)
    reply = shop.handle(1, "start", "/start")
    reply = shop.handle(1, "product", callback=button(reply, "Чёрная футболка"))
    reply = shop.handle(1, "order", callback=button(reply, "Оформить заявку"))
    shop.handle(1, "size", callback=button(reply, "M"))
    shop.handle(1, "name", "Тестовый Покупатель")
    reply = shop.handle(1, "contact", "demo@example.invalid")
    reply = shop.handle(1, "confirm", callback=button(reply, "Подтвердить"))
    return shop, reply.application_id


@pytest.mark.parametrize("state,label", [
    ("pending", "Ожидает отправки"), ("failed", "Ошибка отправки"), ("sent", "Отправлено"),
])
def test_card_and_list_use_readable_labels_without_changing_application(order, state, label):
    shop, application_id = order
    if state != "pending":
        shop.store.notification_result(application_id, "OSError" if state == "failed" else None)
    before = shop.store.get(application_id)
    assert before["original_question"].startswith("Уточнить наличие и размеры:")
    card = shop.handle(999, "view", f"/admin view {application_id}")
    for line in (
        f"Заявка №{application_id}", "Статус: Новая", "Покупатель: Тестовый Покупатель",
        "Контакт: demo@example.invalid", "Товар: Чёрная футболка", "Размер: M",
        f"Уведомление: {label}",
    ):
        assert line in card.text.splitlines()
    assert "Вопрос:" not in card.text
    assert ("Повтор уведомления" in {name for name, _ in card.buttons}) == (state != "sent")
    listing = shop.handle(999, "list", "/admin list")
    assert "Список заявок" in listing.text
    assert "Товар: Чёрная футболка" in listing.text
    assert f"Уведомление: {label}" in listing.text
    assert not any(word in listing.text or word in card.text for word in ("sent", "failed", "pending"))
    pending = shop.handle(999, "pending", "/admin pending")
    assert (f"Заявка №{application_id}" in pending.text) == (state != "sent")
    assert shop.store.get(application_id) == before


def test_real_human_question_is_preserved_after_status_change(order):
    shop, _ = order
    question = "Уточнить наличие и размеры: когда появятся синие джинсы?"
    reply = shop.handle(2, "question", question)
    shop.handle(2, "human", callback=button(reply, "Оставить вопрос человеку"))
    shop.handle(2, "human-name", "Тестовый Покупатель")
    reply = shop.handle(2, "human-contact", "demo@example.invalid")
    result = shop.handle(2, "human-confirm", callback=button(reply, "Подтвердить"))
    shop.handle(999, "processed", f"/admin status {result.application_id} 2")
    before = shop.store.get(result.application_id)
    reply = shop.handle(999, "human-view", f"/admin view {result.application_id}")
    assert f"Вопрос: {question}" in reply.text
    assert "Статус: Обработана" in reply.text
    assert "Товар: Обращение к человеку" in reply.text
    assert "Размер: —" in reply.text
    assert shop.store.get(result.application_id) == before


@pytest.mark.parametrize("index,status", [(0, "Новая"), (1, "Нужен ответ человека"), (2, "Обработана")])
def test_status_confirmation_and_statistics_keep_existing_counts(order, index, status):
    shop, application_id = order
    reply = shop.handle(999, "status", callback=f"admin:status:{application_id}:{index}")
    assert reply.text == f"Заявка №{application_id}: установлен статус «{status}»."
    assert reply.application_id is None
    stats = shop.handle(999, "stats", "/admin stats")
    assert stats.text == (
        "Всего заявок: 1\nПо статусам:\n"
        f"Новая: {int(index == 0)}\nНужен ответ человека: {int(index == 1)}\n"
        f"Обработана: {int(index == 2)}\nПо товарам (до 30 позиций):\nЧёрная футболка: 1"
    )
    assert shop.store.get(application_id)["status"] == status
