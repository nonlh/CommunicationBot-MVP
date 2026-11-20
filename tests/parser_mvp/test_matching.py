import pytest

from parser_mvp import Catalog, MatchConfig, Product, load_catalog, parse_message
from parser_mvp.matching import match_product


def test_exact_name_and_confirmed_alias(parser_catalog):
    for query in ("Футболка синяя", "Модель океан", "Синяя футболка", "футболки синей"):
        result = match_product(query, parser_catalog, MatchConfig())
        assert result["match_status"] == "exact"
        assert result["product_id"] == "TEST-BLUE"


def test_typo_is_only_possible_not_confirmed(parser_catalog):
    result = parse_message("Здравствуйте, сколько стоит футбалка синяя?", parser_catalog)
    assert result["match_status"] == "possible"
    assert result["product_id"] is None
    assert result["candidates"][0]["product_id"] == "TEST-BLUE"
    assert result["needs_clarification"]


def test_similar_products_and_alias_collision_are_ambiguous(parser_catalog):
    result = parse_message("Сколько стоит футболка?", parser_catalog)
    assert result["match_status"] == "ambiguous"
    assert len(result["candidates"]) == 3 and result["product_id"] is None
    collision = Catalog((Product("A", "Первый", ("общий",)), Product("B", "Второй", ("общий",))))
    assert match_product("общий", collision, MatchConfig())["match_status"] == "ambiguous"


def test_color_and_other_modifiers_are_not_silently_discarded(parser_catalog):
    for text in ("Сколько стоит футболка черная?", "Сколько стоит футболка синяя детская?"):
        result = parse_message(text, parser_catalog)
        assert result["product_id"] is None
        assert result["needs_clarification"]
    beige = Catalog((Product("BEIGE", "Футболка бежевая", color="бежевая"),))
    assert match_product("футболка белая", beige, MatchConfig())["match_status"] == "not_found"


def test_unknown_product_and_configurable_threshold(parser_catalog):
    assert parse_message("Сколько стоит самокат?", parser_catalog)["match_status"] == "not_found"
    strict = MatchConfig(fuzzy_threshold=100)
    assert match_product("футбалка синяя", parser_catalog, strict)["match_status"] == "not_found"
    with pytest.raises(ValueError):
        MatchConfig(101)


def test_catalog_reloads_changes_and_does_not_create_products(tmp_path):
    path = tmp_path / "catalog.csv"
    path.write_text("product_id;name;sizes\nONE;Кепка;M\n", encoding="utf-8-sig")
    first = load_catalog(path)
    assert parse_message("Какая цена кепки?", first)["product_id"] is None  # unsupported inflection is not invented
    assert parse_message("Какая цена кепка?", first)["product_id"] == "ONE"
    path.write_text("product_id;name;sizes\nTWO;Кепка;XL\n", encoding="utf-8")
    second = load_catalog(path)
    assert parse_message("Какая цена кепка?", second)["product_id"] == "TWO"
    assert second.products[0].sizes == ("XL",)
    assert first.products[0].product_id == "ONE"


def test_catalog_real_columns_and_validation(tmp_path):
    path = tmp_path / "catalog.csv"
    path.write_text('Артикул,Название,Размеры,Цена\nA,"Товар, первый",XL,123\n', encoding="utf-8")
    assert load_catalog(path).products[0].name == "Товар, первый"
    path.write_text("product_id;name\nA;Первый\nA;Второй\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_catalog(path)
    with pytest.raises(ValueError):
        load_catalog(tmp_path / "catalog.xlsx")
