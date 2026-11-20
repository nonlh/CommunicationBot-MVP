"""Attributes are derived from the supplied catalog, not fixed product categories."""
import pytest

from parser_mvp import Catalog, MatchConfig, Product, load_catalog, parse_message
from parser_mvp.matching import match_product


@pytest.fixture
def catalog():
    return Catalog((
        Product("JACKET", "Синяя куртка", aliases=("куртка синяя",)),
        Product("SKIRT", "Красная юбка"),
        Product("PONCHO", "Серое пончо"),
        Product("VEST", "Фиолетовый жилет", color="фиолетовый"),
        Product("HIDDEN", "Красная куртка", active=False),
    ))


@pytest.mark.parametrize("query,expected", [
    ("СИНЯЯ КУРТКА", "JACKET"),
    ("куртка синяя", "JACKET"),
    ("красная юбка", "SKIRT"),
    ("серое пончо", "PONCHO"),
    ("фиолетовый жилет", "VEST"),
    ("фиолетового жилет", "VEST"),
])
def test_exact_categories_and_color_forms_from_catalog(catalog, query, expected):
    result = match_product(query, catalog, MatchConfig())
    assert result["match_status"] == "exact"
    assert result["product_id"] == expected


@pytest.mark.parametrize("query", [
    "красная куртка", "синяя юбка", "серый жилет", "фиолетовое пончо",
    "синяя сумка", "оранжевая куртка", "красная куркта",
])
def test_joint_attributes_cannot_be_overridden_by_fuzzy(catalog, query):
    result = match_product(query, catalog, MatchConfig(fuzzy_threshold=10))
    assert result == {"match_status": "not_found", "product_id": None, "candidates": []}


def test_new_catalog_record_enables_previously_missing_combination(tmp_path):
    path = tmp_path / "catalog.csv"
    path.write_text("product_id;name\nA;Синяя куртка\nB;Красная юбка\n", encoding="utf-8")
    assert parse_message("Есть красная куртка?", load_catalog(path))["match_status"] == "not_found"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("C;Красная куртка\n")
    result = parse_message("Здравствуйте, а есть красная куртка?", load_catalog(path))
    assert (result["intent"], result["match_status"], result["product_id"]) == ("AVAILABILITY", "exact", "C")


def test_typo_single_category_and_empty_catalog():
    catalog = Catalog((Product("A", "Синяя куртка"),))
    result = match_product("синяя куркта", catalog, MatchConfig())
    assert result["match_status"] == "possible"
    assert result["product_id"] is None
    assert result["candidates"][0]["product_id"] == "A"
    assert match_product("куртка", Catalog(()), MatchConfig())["match_status"] == "not_found"
