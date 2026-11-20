"""Type and color constrain one candidate jointly, before fuzzy ranking."""
import pytest

from parser_mvp import MatchConfig, load_catalog, parse_message
from parser_mvp.catalog import DEFAULT_CATALOG_PATH
from parser_mvp.matching import match_product


@pytest.fixture
def catalog():
    return load_catalog(DEFAULT_CATALOG_PATH)


@pytest.mark.parametrize("query,status,product_id,candidates", [
    ("серая футболка", "not_found", None, []),
    ("чёрная футболка", "exact", "T01", ["T01"]),
    ("серое худи", "exact", "T02", ["T02"]),
    ("синие джинсы", "exact", "T03", ["T03"]),
    ("чорная футблка", "possible", None, ["T01"]),
    ("красная футболка", "not_found", None, []),
])
def test_required_product_combinations(catalog, query, status, product_id, candidates):
    result = match_product(query, catalog, MatchConfig())
    assert result["match_status"] == status
    assert result["product_id"] == product_id
    assert [candidate["product_id"] for candidate in result["candidates"]] == candidates


@pytest.mark.parametrize("text,product_id,status", [
    ("здравствуйте а сколько стоит серая футболка?", None, "not_found"),
    ("Здравствуйте, а сколько стоит чёрная футболка?", "T01", "exact"),
    ("А сколько стоит серое худи?", "T02", "exact"),
    ("сколько стоит синие джинсы?", "T03", "exact"),
    ("сколько стоит чорная футблка?", None, "possible"),
    ("сколько стоит красная футболка?", None, "not_found"),
])
def test_price_intent_with_product_features(catalog, text, product_id, status):
    result = parse_message(text, catalog)
    assert result["intent"] == "PRICE"
    assert result["product_id"] == product_id
    assert result["match_status"] == status
    assert result["needs_clarification"] == (status != "exact")


@pytest.mark.parametrize("query", [
    "серая футболка", "чёрное худи", "синяя футболка", "красная футболка",
    "серая футблка", "чорное худи",
])
def test_low_fuzzy_threshold_cannot_override_color_or_type(catalog, query):
    result = match_product(query, catalog, MatchConfig(fuzzy_threshold=10))
    assert result["match_status"] == "not_found"
    assert not result["candidates"]


@pytest.mark.parametrize("text", [
    "сколько стоит доставка футболки?",
    "здравствуйте а сколько стоит доставка футболки?",
    "А сколько стоит доставка чёрной футболки?",
    "Не хочу купить чёрную футболку",
    "Сколько стоит чёрная футболка и какие размеры?",
])
def test_prefix_support_preserves_unsupported_and_ambiguous_questions(catalog, text):
    result = parse_message(text, catalog)
    assert result["intent"] == "UNKNOWN"
    assert result["product_id"] is None


def test_typo_remains_unconfirmed_and_does_not_inherit_context(catalog):
    result = parse_message(
        "Сколько стоит чорная футблка?", catalog, {"product_id": "T02"},
    )
    assert result["product_id"] is None
    assert result["match_status"] == "possible"
    assert result["candidates"][0]["product_id"] == "T01"
    assert result["price"] is None


def test_missing_combination_is_not_replaced_by_context(catalog):
    result = parse_message(
        "здравствуйте а сколько стоит серая футболка?", catalog, {"product_id": "T01"},
    )
    assert result["intent"] == "PRICE"
    assert result["match_status"] == "not_found"
    assert result["product_id"] is None
    assert not result["candidates"]
