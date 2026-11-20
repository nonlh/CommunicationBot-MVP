"""Regression coverage for information, greetings and conservative size input."""
from copy import deepcopy

import pytest

from parser_mvp import load_catalog
from parser_mvp.catalog import DEFAULT_CATALOG_PATH
from store_core.service import REPEAT_CONFIRMATION, Shop
from store_core.simulator import button
from store_core.storage import Store


@pytest.fixture
def shop(tmp_path):
    return Shop(load_catalog(DEFAULT_CATALOG_PATH), Store(tmp_path / "dialog.sqlite3"), 999)


def session(shop):
    with shop.store.connection() as db:
        return shop.store.session(db, 1)


def form_data(shop):
    return {key: value for key, value in session(shop).items() if key != "buttons"}


def reach(shop, stage):
    reply = shop.handle(1, "start", "/start")
    if stage == "product":
        return reply
    if stage == "cancelled":
        return shop.handle(1, "cancel", "/cancel")
    if stage == "human_offer":
        return shop.handle(1, "question", "Какие условия доставки?")
    reply = shop.handle(1, "order", "Хочу купить чёрную футболку")
    if stage == "size":
        return reply
    reply = shop.handle(1, "size", "M")
    if stage == "name":
        return reply
    reply = shop.handle(1, "name", "Тестовый Покупатель")
    if stage == "contact":
        return reply
    reply = shop.handle(1, "contact", "demo@example.invalid")
    if stage == "confirm":
        return reply
    return shop.handle(1, "confirm", callback=button(reply, "Подтвердить"))


STAGES = ("product", "size", "name", "contact", "confirm", "human_offer", "done", "cancelled")


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("text,expected", [
    ("Здравствуйте! Сколько стоит чёрная футболка?", "2490 RUB"),
    ("Какие размеры серого худи?", "Размеры в наличии: M, L"),
    ("Есть синие джинсы?", "нет в наличии"),
])
def test_information_does_not_start_or_replace_checkout(shop, stage, text, expected):
    reach(shop, stage)
    before = deepcopy(form_data(shop))
    applications = shop.store.applications()
    reply = shop.handle(1, "info", text)
    assert expected in reply.text
    assert reply.application_id is None
    assert REPEAT_CONFIRMATION not in reply.text
    assert session(shop)["stage"] == stage
    if stage not in ("product", "done", "cancelled"):
        assert form_data(shop) == before
    else:
        assert "Выберите размер" not in reply.text
        assert "Введите имя" not in reply.text
        assert not {"S", "M", "L"} & {label for label, _ in reply.buttons}
    assert shop.store.applications() == applications


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("greeting", ["Здравствуйте", "Привет!"])
def test_greeting_does_not_confirm_reset_or_fill_a_form(shop, stage, greeting):
    reach(shop, stage)
    before = deepcopy(form_data(shop))
    applications = shop.store.applications()
    reply = shop.handle(1, "greeting", greeting)
    assert "Здравствуйте" in reply.text
    assert "зарегистрировано" not in reply.text
    assert "отсутствует" not in reply.text
    assert reply.application_id is None
    assert form_data(shop) == before
    assert shop.store.applications() == applications


@pytest.mark.parametrize("text", ["спасибо", "не знаю", "Подтвердить", "что-то непонятное"])
def test_unknown_size_text_does_not_claim_missing_stock(shop, text):
    reach(shop, "size")
    before = form_data(shop)
    reply = shop.handle(1, "unknown", text)
    assert "отсутствует" not in reply.text
    assert "не указан" not in reply.text
    assert "продолжить оформление" in reply.text
    assert {"M", "Каталог", "Новое обращение"} <= {label for label, _ in reply.buttons}
    assert form_data(shop) == before
    assert not shop.store.applications()


@pytest.mark.parametrize("text", ["XL", "размер XL", "42", "размер 42", "XXXL"])
def test_explicit_missing_size_is_rejected_without_reset(shop, text):
    reach(shop, "size")
    before = form_data(shop)
    reply = shop.handle(1, "missing", text)
    assert "отсутствует в наличии" in reply.text
    assert form_data(shop) == before
    assert not shop.store.applications()


@pytest.mark.parametrize("text", ["M", "m", "размер M"])
def test_available_size_still_advances_only_an_explicit_order(shop, text):
    reach(shop, "size")
    shop.handle(1, "available", text)
    assert session(shop)["stage"] == "name"
    assert session(shop)["size"] == "M"
    assert not shop.store.applications()


@pytest.mark.parametrize("text", [
    "Сколько стоит чёрная футбалка?", "Какие размеры чёрная футбалка?",
    "Есть чёрная футбалка M?",
])
def test_information_candidate_selection_does_not_become_an_order(shop, text):
    reply = shop.handle(1, "info", text)
    assert "Подтвердите товар" in reply.text
    reply = shop.handle(1, "choice", callback=button(reply, "Чёрная футболка"))
    assert session(shop)["stage"] == "product"
    assert not {"S", "M", "L"} & {label for label, _ in reply.buttons}
    assert button(reply, "Оформить заявку")
    shop.handle(1, "explicit-order", callback=button(reply, "Оформить заявку"))
    assert session(shop)["stage"] == "size"
    assert not shop.store.applications()


def test_catalog_selection_is_information_until_order_button(shop):
    reply = reach(shop, "product")
    reply = shop.handle(1, "choose", callback=button(reply, "Чёрная футболка"))
    assert session(shop)["stage"] == "product"
    assert "Выберите размер" not in reply.text
    reply = shop.handle(1, "order-button", callback=button(reply, "Оформить заявку"))
    assert session(shop)["stage"] == "size"
    assert button(reply, "M")


def test_completed_order_greeting_and_restart_keep_real_confirmation_idempotent(shop):
    preview = reach(shop, "confirm")
    confirmation = button(preview, "Подтвердить")
    first = shop.handle(1, "confirm", callback=confirmation)
    shop.handle(1, "greeting", "Здравствуйте")
    reply = shop.handle(1, "info", "Сколько стоит серое худи?")
    assert reply.application_id is None
    restarted = Shop(shop.catalog, Store(shop.store.path), 999)
    repeated = restarted.handle(1, "confirm-again", callback=confirmation)
    assert repeated.text == REPEAT_CONFIRMATION
    assert repeated.application_id == first.application_id
    assert len(restarted.store.applications()) == 1
    restarted.handle(1, "new-order", callback=button(reply, "Оформить заявку"))
    assert session(restarted)["stage"] == "size"
    assert session(restarted)["product_id"] == "T02"
    assert session(restarted)["operation"] != shop.store.get(first.application_id)["operation_id"]


def test_arbitrary_old_button_is_not_confirmation_replay(shop):
    preview = reach(shop, "confirm")
    old_back = button(preview, "Назад")
    confirmation = button(preview, "Подтвердить")
    first = shop.handle(1, "confirm", callback=confirmation)
    reply = shop.handle(1, "old-back", callback=old_back)
    assert "устарела" in reply.text
    assert reply.application_id is None
    assert len(shop.store.applications()) == 1
    assert shop.handle(1, "real-repeat", callback=confirmation).application_id == first.application_id


def test_confirmation_button_remains_valid_after_information_and_greeting(shop):
    preview = reach(shop, "confirm")
    original_confirmation = button(preview, "Подтвердить")
    before = form_data(shop)
    shop.handle(1, "other-product", "Какая цена серого худи?")
    shop.handle(1, "greeting", "Здравствуйте")
    assert form_data(shop) == before
    reply = shop.handle(1, "confirm", callback=original_confirmation)
    assert shop.store.get(reply.application_id)["product_id"] == "T01"
    assert shop.store.get(reply.application_id)["size"] == "M"


def test_t03_information_never_offers_standard_order(shop):
    reply = shop.handle(1, "t03", "Сколько стоят синие джинсы?")
    assert "6490 RUB" in reply.text
    assert "Оформить заявку" not in {label for label, _ in reply.buttons}
    assert session(shop)["stage"] == "product"
    assert not shop.store.applications()


@pytest.mark.parametrize("stage", ["size", "name", "contact", "confirm"])
def test_missing_combination_answer_preserves_unfinished_form(shop, stage):
    reach(shop, stage)
    before = form_data(shop)
    reply = shop.handle(1, "missing-combination", "здравствуйте а сколько стоит серая футболка?")
    assert "Такого товара нет в каталоге." in reply.text
    assert "ответ человека" not in reply.text
    assert form_data(shop) == before
    assert not shop.store.applications()
