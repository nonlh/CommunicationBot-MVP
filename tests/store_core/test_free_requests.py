"""Free request word order reaches existing service routes without changing checkout."""
import pytest

from parser_mvp import Catalog, Product, load_catalog, parse_message
from parser_mvp.catalog import DEFAULT_CATALOG_PATH
from store_core.service import Shop
from store_core.simulator import button
from store_core.storage import Store


@pytest.fixture
def shop(tmp_path):
    return Shop(load_catalog(DEFAULT_CATALOG_PATH), Store(tmp_path / "free.sqlite3"))


def session(shop):
    with shop.store.connection() as db:
        return shop.store.session(db, 1)


@pytest.mark.parametrize("text", [
    "Мне нужна футболка XL, очень срочно!",
    "Нужна футболка в размере XL, срочно!",
    "Мне нужны футболки размера XL, пожалуйста!",
])
def test_category_only_request_keeps_size_through_product_confirmation(shop, text):
    shop.handle(1, "start", "/start")
    parsed = parse_message(text, shop.catalog)
    assert parsed["intent"] == "AVAILABILITY"
    assert parsed["requested_size"] == "XL"
    assert parsed["match_status"] == "possible"
    assert [c["product_id"] for c in parsed["candidates"]] == ["T01"]
    reply = shop.handle(1, "request", text)
    assert "Подтвердите товар" in reply.text
    assert "человек" not in reply.text
    assert session(shop)["stage"] == "product"
    reply = shop.handle(1, "choose", callback=button(reply, "Чёрная футболка"))
    assert "Запрошенный размер отсутствует в наличии" in reply.text
    assert "Размеры в наличии: S, M, L" in reply.text
    assert session(shop)["stage"] == "product"
    assert not shop.store.applications()


@pytest.mark.parametrize("text", [
    "А серое худи можно в размере S?",
    "Серое худи можно в размере s?",
    "Здравствуйте, серое худи можно размером S, пожалуйста?",
    "Мне нужно серое худи в размере S!",
])
def test_named_product_and_unavailable_size_are_informational(shop, text):
    shop.handle(1, "start", "/start")
    parsed = parse_message(text, shop.catalog)
    assert (parsed["intent"], parsed["product_id"], parsed["requested_size"], parsed["size_status"]) == (
        "AVAILABILITY", "T02", "S", "unavailable",
    )
    reply = shop.handle(1, "request", text)
    assert "Серое худи" in reply.text
    assert "Запрошенный размер отсутствует в наличии" in reply.text
    assert "Размеры в наличии: M, L" in reply.text
    assert "человек" not in reply.text
    assert "Введите имя" not in reply.text
    assert session(shop)["stage"] == "product"
    assert not shop.store.applications()


@pytest.mark.parametrize("text", [
    "Синее худи L хочу заказать",
    "Синего худи в размере L хотел бы купить!",
    "Синее худи размером L хотела бы заказать, пожалуйста",
])
def test_trailing_order_intent_rejects_conflicting_color_and_category(shop, text):
    shop.handle(1, "start", "/start")
    parsed = parse_message(text, shop.catalog)
    assert parsed["intent"] == "ORDER"
    assert parsed["requested_size"] == "L"
    assert parsed["match_status"] == "not_found"
    assert parsed["product_id"] is None
    reply = shop.handle(1, "request", text)
    assert reply.text == "Такого товара нет в каталоге."
    assert not any(label in ("Оформить заявку", "Оставить вопрос человеку") for label, _ in reply.buttons)
    assert session(shop)["stage"] == "product"
    assert not shop.store.applications()


@pytest.mark.parametrize("text", [
    "Мне нужна футболка XL с доставкой, срочно!",
    "Серое худи можно в размере S с оплатой при получении?",
    "Мне нужен состав ткани серого худи",
    "Синее худи L не хочу заказать",
    "Мне нужна футболка и худи",
    "Серое худи можно в размере S? Когда будет доставка?",
])
def test_free_request_patterns_keep_unknown_facts_and_negations_unsupported(shop, text):
    shop.handle(1, "start", "/start")
    assert parse_message(text, shop.catalog)["intent"] == "UNKNOWN"
    reply = shop.handle(1, "request", text)
    assert "ответ человека" in reply.text
    assert not shop.store.applications()


@pytest.mark.parametrize("stage", ["size", "name", "contact", "confirm"])
@pytest.mark.parametrize("text", [
    "Мне нужна футболка XL, очень срочно!",
    "А серое худи можно в размере S?",
    "Синее худи L хочу заказать",
])
def test_free_request_never_switches_checkout_product(shop, stage, text):
    shop.handle(1, "order", "Хочу купить чёрную футболку")
    if stage != "size":
        shop.handle(1, "size", "M")
    if stage in ("contact", "confirm"):
        shop.handle(1, "name", "Тестовый Покупатель")
    if stage == "confirm":
        shop.handle(1, "contact", "demo@example.invalid")
    before = session(shop)
    shop.handle(1, "request", text)
    after = session(shop)
    for field in ("operation", "product_id", "product_name", "size"):
        assert after.get(field) == before.get(field)
    if parse_message(text, shop.catalog)["intent"] == "AVAILABILITY":
        assert after["stage"] == before["stage"]
    assert not shop.store.applications()


def test_existing_navigation_still_allows_explicit_product_change(shop):
    shop.handle(1, "order", "Хочу купить чёрную футболку")
    reply = shop.handle(1, "question", "А серое худи можно в размере S?")
    assert session(shop)["product_id"] == "T01"
    assert button(reply, "Каталог") == "ui:catalog"
    # Telegram maps this existing navigation action to /catalog.
    reply = shop.handle(1, "catalog", "/catalog")
    reply = shop.handle(1, "product", callback=button(reply, "Серое худи"))
    shop.handle(1, "new-order", callback=button(reply, "Оформить заявку"))
    assert (session(shop)["stage"], session(shop)["product_id"]) == ("size", "T02")
    assert not shop.store.applications()


def test_category_only_still_requires_disambiguation_for_multiple_products(shop):
    shop.catalog = Catalog((*shop.catalog.products, Product("TEST", "Белая футболка", sizes=("M",))))
    shop.handle(1, "start", "/start")
    reply = shop.handle(1, "request", "Мне нужна футболка XL, срочно")
    assert "Подтвердите товар" in reply.text
    assert button(reply, "Чёрная футболка")
    assert button(reply, "Белая футболка")
    assert session(shop).get("product_id") is None
    assert not shop.store.applications()


def test_supported_trailing_order_uses_existing_checkout(shop):
    shop.handle(1, "start", "/start")
    reply = shop.handle(1, "request", "Серое худи в размере M хочу заказать")
    assert "Введите имя покупателя" in reply.text
    assert (session(shop)["stage"], session(shop)["product_id"], session(shop)["size"]) == ("name", "T02", "M")
    assert not shop.store.applications()
