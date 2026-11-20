"""One regression for JSON, customer replies, stock semantics and persisted FSM."""
import json
from decimal import Decimal

import pytest

from parser_mvp import Catalog, Product, load_catalog, parse_message
from parser_mvp.catalog import DEFAULT_CATALOG_PATH
from store_core.service import Shop
from store_core.simulator import button
from store_core.storage import Store


CASES = [
    ("Есть серая футболка?", "AVAILABILITY", "not_found", None, "Такого товара нет в каталоге.", "product"),
    ("Здравствуйте, а есть серая футболка?", "AVAILABILITY", "not_found", None, "Такого товара нет в каталоге.", "product"),
    ("Есть чёрное худи?", "AVAILABILITY", "not_found", None, "Такого товара нет в каталоге.", "product"),
    ("Есть чёрные джинсы?", "AVAILABILITY", "not_found", None, "Такого товара нет в каталоге.", "product"),
    ("Сколько стоит синяя футболка?", "PRICE", "not_found", None, "Такого товара нет в каталоге.", "product"),
    ("Сколько стоит чёрная футболка?", "PRICE", "exact", "T01", "2490 RUB", "product"),
    ("Есть чорная футблка?", "AVAILABILITY", "possible", None, "Подтвердите товар", "product"),
    ("Можно заказать синие джинсы?", "ORDER", "exact", "T03", "Сейчас товара нет в наличии.", "product"),
    ("Есть синие джинсы M?", "AVAILABILITY", "exact", "T03", "Сейчас товара нет в наличии.", "product"),
    ("Сколько стоят синие джинсы?", "PRICE", "exact", "T03", "6490 RUB", "product"),
    ("Когда появятся синие джинсы?", "UNKNOWN", "not_applicable", None, "ответ человека", "human_offer"),
    ("Хочу чёрную футболку XL", "ORDER", "exact", "T01", "размер отсутствует в наличии", "size"),
    ("Хочу серое худи M", "ORDER", "exact", "T02", "Введите имя покупателя", "name"),
    ("Сколько стоит доставка худи?", "UNKNOWN", "not_applicable", None, "ответ человека", "human_offer"),
]


@pytest.fixture
def shop(tmp_path):
    return Shop(load_catalog(DEFAULT_CATALOG_PATH), Store(tmp_path / "test.sqlite3"))


@pytest.mark.parametrize("text,intent,status,product_id,expected,stage", CASES)
def test_required_json_and_final_reply(shop, text, intent, status, product_id, expected, stage):
    parsed = json.loads(json.dumps(parse_message(text, shop.catalog)))
    assert (parsed["intent"], parsed["match_status"], parsed["product_id"]) == (intent, status, product_id)
    reply = shop.handle(1, "question", text)
    assert expected in reply.text
    with shop.store.connection() as db:
        assert shop.store.session(db, 1)["stage"] == stage
    assert not shop.store.applications()
    assert reply.application_id is None
    if status == "not_found" or parsed["availability"] == "out_of_stock":
        labels = {label for label, _ in reply.buttons}
        assert "Каталог" in labels
        assert not labels & {"Оставить вопрос человеку", "Оформить заявку", "Подтвердить", "M"}
        assert "человек" not in reply.text
        assert "уточнить" not in reply.text
    if product_id == "T03":
        assert reply.text == "Синие джинсы\nЦена: 6490 RUB\nСейчас товара нет в наличии."
        assert parsed["available_sizes"] == []
        assert parsed["availability"] == "out_of_stock"
    if status == "possible":
        assert button(reply, "Чёрная футболка")
        assert "2490" not in reply.text
        confirmed = shop.handle(1, "choose", callback=button(reply, "Чёрная футболка"))
        assert "2490 RUB" in confirmed.text
        assert button(confirmed, "Оформить заявку")


@pytest.mark.parametrize("stage", ["size", "name", "contact", "confirm"])
@pytest.mark.parametrize("question", ["Есть серая футболка?", "Есть синие джинсы M?"])
def test_information_does_not_reset_unfinished_order(shop, stage, question):
    reply = shop.handle(1, "order", "Хочу купить чёрную футболку")
    if stage in ("name", "contact", "confirm"):
        shop.handle(1, "size", callback=button(reply, "M"))
    if stage in ("contact", "confirm"):
        shop.handle(1, "name", "Тестовый Покупатель")
    if stage == "confirm":
        shop.handle(1, "contact", "demo@example.invalid")
    with shop.store.connection() as db:
        before = shop.store.session(db, 1)
    reply = shop.handle(1, "question", question)
    assert ("Такого товара нет в каталоге." if "серая" in question else "Сейчас товара нет в наличии.") in reply.text
    with shop.store.connection() as db:
        after = shop.store.session(db, 1)
    assert {k: v for k, v in before.items() if k != "buttons"} == {k: v for k, v in after.items() if k != "buttons"}
    assert not shop.store.applications()


@pytest.mark.parametrize("known,sizes,availability", [
    (True, ("M",), "in_stock"), (True, (), "out_of_stock"), (False, (), "unknown"),
])
def test_stock_semantics_are_catalog_driven(tmp_path, known, sizes, availability):
    catalog = Catalog((Product("NEW", "Красная куртка", sizes=sizes,
                               price=Decimal("123"), currency="RUB", stock_known=known),))
    shop = Shop(catalog, Store(tmp_path / "stock.sqlite3"))
    parsed = parse_message("Есть красная куртка?", catalog)
    assert parsed["availability"] == availability
    reply = shop.handle(1, "info", "Есть красная куртка?")
    order = shop.handle(2, "order", "Хочу купить красную куртку")
    if availability == "out_of_stock":
        for result in (reply, order):
            assert result.text == "Красная куртка\nЦена: 123 RUB\nСейчас товара нет в наличии."
            assert "человек" not in result.text
            assert not any("человек" in label for label, _ in result.buttons)
    elif availability == "unknown":
        assert "наличие уточнит человек" in reply.text
        assert button(order, "Оставить вопрос человеку")
    else:
        assert button(order, "M")
        assert "Сейчас товара нет" not in reply.text
    assert not shop.store.applications()
