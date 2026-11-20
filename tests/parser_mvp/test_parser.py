import json
from copy import deepcopy

import pytest

from parser_mvp import parse_message


@pytest.mark.parametrize("text,intent,product,size", [
    ("Сколько стоит футболка синяя?", "PRICE", "TEST-BLUE", None),
    ("ПРИВЕТ! СКОЛЬКО СТОИТ ФУТБОЛКА СИНЯЯ?", "PRICE", "TEST-BLUE", None),
    ("  Какая   цена рубашки зеленой? ", "PRICE", "TEST-GREEN", None),
    ("Подскажите, какие размеры у рубашки зелёной?", "SIZES", "TEST-GREEN", None),
    ("Есть ли у вас футболка синяя размер xl?", "AVAILABILITY", "TEST-BLUE", "XL"),
    ("Есть футболка синяя XXL?", "AVAILABILITY", "TEST-BLUE", "XXL"),
    ("Футболка синяя в наличии?", "AVAILABILITY", "TEST-BLUE", None),
    ("Хочу заказать футболку синюю XXL", "ORDER", "TEST-BLUE", "XXL"),
    ("Хочу купить платье Луна размер 42", "ORDER", "TEST-DRESS", "42"),
    ("Покажи каталог", "CATALOG", None, None),
    ("Пришлите каталог пожалуйста", "CATALOG", None, None),
])
def test_supported_intents_and_normalization(parser_catalog, text, intent, product, size):
    result = parse_message(text, parser_catalog)
    assert result["intent"] == intent
    assert result["product_id"] == product
    assert result["size"] == size
    assert result["needs_clarification"] is False
    assert {"intent", "product_id", "size", "match_status", "needs_clarification", "reason"} <= result.keys()
    assert result["reason"]
    json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("text", [
    "Сколько стоит доставка футболки?",
    "Какая цена доставки синей футболки?",
    "Цена футболки с доставкой?",
    "Из какого материала футболка?",
    "Хочу не заказать футболку синюю",
    "Не хочу купить футболку синюю",
    "Сколько стоит футболка синяя и какие размеры?",
    "Сколько стоит футболка синяя? Из какого материала она?",
    "Сколько стоит футболка синяя из какого материала она",
    "Цена футболки синей, размеры XL",
    "Хочу заказать футболку синюю и рубашку зеленую",
    "Сколько стоит не футболка а рубашка?",
    "Футболка синяя нравится", "", "Добрый день!",
])
def test_unsupported_or_ambiguous_semantics_are_unknown(parser_catalog, text):
    result = parse_message(text, parser_catalog)
    assert result["intent"] == "UNKNOWN"
    assert result["product_id"] is None
    assert result["needs_clarification"] is True


@pytest.mark.parametrize("text", ["Сколько стоит?", "Какие размеры?", "Хочу заказать"])
def test_missing_product_needs_clarification(parser_catalog, text):
    result = parse_message(text, parser_catalog)
    assert result["match_status"] == "not_applicable"
    assert result["product_id"] is None
    assert result["needs_clarification"]


def test_sizes_do_not_substitute_or_claim_stock(parser_catalog):
    unknown = parse_message("Есть футболка синяя размер Z9", parser_catalog)
    assert unknown["size"] is None and unknown["needs_clarification"]
    assert "неизвестен" in unknown["reason"]
    unsupported = parse_message("Есть футболка белая XXL", parser_catalog)
    assert unsupported["size"] == "XXL" and unsupported["needs_clarification"]
    assert "наличие не определено" in unsupported["reason"]
    numeric = parse_message("Есть платье Луна размер 43", parser_catalog)
    assert numeric["size"] is None and numeric["needs_clarification"]
    multiple = parse_message("Есть футболка синяя XL XXL", parser_catalog)
    assert multiple["size"] is None and multiple["needs_clarification"]


def test_validated_context_only_used_when_product_is_omitted(parser_catalog):
    state = {"product_id": "TEST-BLUE"}
    assert parse_message("Какая цена?", parser_catalog, state)["product_id"] == "TEST-BLUE"
    assert parse_message("Какая цена самоката?", parser_catalog, state)["product_id"] is None
    assert parse_message("Какая цена?", parser_catalog, {"product_id": "deleted"})["needs_clarification"]
    assert parse_message("XL", parser_catalog, "awaiting_size")["intent"] == "UNKNOWN"


def test_parser_is_pure_and_deterministic(parser_catalog, monkeypatch):
    before = deepcopy(parser_catalog)
    state = {"product_id": "TEST-BLUE"}
    snapshot = deepcopy(state)
    def deny(*args, **kwargs):
        raise AssertionError("Parser must not write files or use network")
    monkeypatch.setattr("builtins.open", deny)
    monkeypatch.setattr("socket.socket", deny)
    first = parse_message("Есть футболка синяя XL?", parser_catalog, state)
    assert first == parse_message("Есть футболка синяя XL?", parser_catalog, state)
    assert parser_catalog == before and state == snapshot
