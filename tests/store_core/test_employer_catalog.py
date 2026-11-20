"""Employer's three-product catalog is the sole source for these checks."""
import json
import csv
import subprocess
import sys
from decimal import Decimal

import pytest
from openpyxl import Workbook

from parser_mvp import load_catalog, parse_message
from parser_mvp.catalog import DEFAULT_CATALOG_PATH
from parser_mvp.batch import run_batch
from store_core.config import Settings
from store_core.service import Shop
from store_core.simulator import button
from store_core.storage import Store


@pytest.fixture
def shop(tmp_path):
    return Shop(load_catalog(DEFAULT_CATALOG_PATH), Store(tmp_path / "store.sqlite3"), 999)


def test_exactly_three_supplied_products_and_defaults(monkeypatch):
    monkeypatch.delenv("CATALOG_PATH", raising=False)
    assert Settings().catalog_path == str(DEFAULT_CATALOG_PATH)
    assert Settings.from_env().catalog_path == str(DEFAULT_CATALOG_PATH)
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    assert [(p.product_id, p.name, p.price, p.currency, p.sizes) for p in catalog.products] == [
        ("T01", "Чёрная футболка", Decimal("2490"), "RUB", ("S", "M", "L")),
        ("T02", "Серое худи", Decimal("5990"), "RUB", ("M", "L")),
        ("T03", "Синие джинсы", Decimal("6490"), "RUB", ()),
    ]
    assert all(p.stock_known and p.active for p in catalog.products)
    assert len(catalog.visible_products) == 3


@pytest.mark.parametrize("text,product_id,price,sizes,availability", [
    ("Сколько стоит чёрная футболка?", "T01", "2490", ["S", "M", "L"], "in_stock"),
    ("Какие размеры серого худи?", "T02", "5990", ["M", "L"], "in_stock"),
    ("Есть синие джинсы?", "T03", "6490", [], "out_of_stock"),
    ("Какая цена синих джинсов?", "T03", "6490", [], "out_of_stock"),
    ("Какая цена T01?", "T01", "2490", ["S", "M", "L"], "in_stock"),
])
def test_parser_returns_only_supplied_facts(text, product_id, price, sizes, availability):
    result = parse_message(text, load_catalog(DEFAULT_CATALOG_PATH))
    assert result["product_id"] == product_id
    assert result["price"] == price
    assert result["currency"] == "RUB"
    assert result["available_sizes"] == sizes
    assert result["availability"] == availability
    json.dumps(result)


@pytest.mark.parametrize("question,expected", [
    ("Сколько стоит чёрная футболка?", "2490 RUB"),
    ("Какие размеры серого худи?", "Размеры в наличии: M, L"),
    ("Есть чёрная футболка S?", "Наличие по каталогу: в наличии"),
    ("Сколько стоят синие джинсы?", "6490 RUB"),
])
def test_shop_answers_catalog_questions(shop, question, expected):
    reply = shop.handle(1, question, question)
    assert expected in reply.text
    assert "наличие уточнит" not in reply.text
    assert "Остатки требуют" not in reply.text
    assert not shop.store.applications()


@pytest.mark.parametrize("question", [
    "Хочу купить чёрную футболку XL",
    "Есть серое худи S?",
    "Хочу купить синие джинсы M",
    "Хочу купить синие джинсы размер 42",
])
def test_missing_size_never_reaches_standard_confirmation(shop, question):
    parsed = parse_message(question, shop.catalog)
    assert parsed["size_status"] == "unavailable"
    assert parsed["needs_clarification"]
    reply = shop.handle(1, "question", question)
    out_of_stock = parsed["availability"] == "out_of_stock"
    assert ("Сейчас товара нет в наличии" if out_of_stock else "отсутствует в наличии") in reply.text
    assert not any(label == "Подтвердить" for label, _ in reply.buttons)
    with shop.store.connection() as db:
        assert shop.store.session(db, 1)["stage"] in (
            ("product",) if parsed["intent"] != "ORDER" or out_of_stock else ("size",)
        )
    assert not shop.store.applications()


def test_t03_visible_with_price_and_zero_size_choices(shop):
    reply = shop.handle(1, "start", "/start")
    reply = shop.handle(1, "choose", callback=button(reply, "Синие джинсы"))
    assert "6490 RUB" in reply.text
    assert "нет в наличии" in reply.text
    assert reply.text == "Синие джинсы\nЦена: 6490 RUB\nСейчас товара нет в наличии."
    assert not any("человек" in label for label, _ in reply.buttons)
    assert "Выберите размер" not in reply.text
    assert not {"S", "M", "L", "XL"} & {label for label, _ in reply.buttons}
    shop.handle(1, "invented-size", "M")
    shop.handle(1, "fake-confirm", "Подтвердить")
    assert not shop.store.applications()


@pytest.mark.parametrize("question", [
    "Из какого материала чёрная футболка?",
    "Какой состав ткани серого худи?",
    "Сколько стоит доставка чёрной футболки?",
    "Какие условия оплаты?",
    "Как оформить возврат серого худи?",
    "Сколько стоит оплата чёрной футболки?",
])
def test_unknown_facts_offer_human_and_preserve_question(shop, question):
    assert parse_message(question, shop.catalog)["intent"] == "UNKNOWN"
    reply = shop.handle(1, "question", question)
    assert "ответ человека" in reply.text
    shop.handle(1, "human", callback=button(reply, "Оставить вопрос человеку"))
    shop.handle(1, "name", "Тестовый Покупатель")
    reply = shop.handle(1, "contact", "demo@example.invalid")
    assert not shop.store.applications()
    reply = shop.handle(1, "confirm", callback=button(reply, "Подтвердить"))
    row = shop.store.get(reply.application_id)
    assert row["original_question"] == question
    assert row["status"] == "Нужен ответ человека"


def test_unknown_question_during_size_selection_is_escalated(shop):
    reply = shop.handle(1, "start", "/start")
    reply = shop.handle(1, "product", callback=button(reply, "Чёрная футболка"))
    shop.handle(1, "order", callback=button(reply, "Оформить заявку"))
    question = "Какой состав ткани?"
    reply = shop.handle(1, "question", question)
    assert "ответ человека" in reply.text
    with shop.store.connection() as db:
        session = shop.store.session(db, 1)
        assert session["stage"] == "size"
        assert session["pending_question"] == question


def test_missing_product_is_not_created_or_substituted(shop):
    reply = shop.handle(1, "missing", "Хочу купить T99")
    assert reply.text == "Такого товара нет в каталоге."
    assert not any("человек" in label for label, _ in reply.buttons)
    assert len(shop.catalog.products) == 3
    assert not shop.store.applications()


def test_legacy_empty_sizes_does_not_mean_confirmed_out_of_stock():
    catalog = load_catalog("tests/fixtures/store_catalog.csv")
    assert all(not product.stock_known for product in catalog.products)
    assert parse_message("Есть футболка белая?", catalog)["availability"] == "unknown"


def test_xlsx_explicit_stock_column_and_duplicate_semantics(tmp_path):
    path = tmp_path / "stock.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["id", "name", "размеры в наличии"])
    sheet.append(["T03", "Синие джинсы", None])
    workbook.save(path)
    catalog = load_catalog(path)
    assert catalog.products[0].availability == "out_of_stock"
    sheet.cell(row=1, column=4, value="sizes")
    sheet.cell(row=2, column=4, value="M")
    workbook.save(path)
    workbook.close()
    with pytest.raises(ValueError, match="колонки"):
        load_catalog(path)


def test_parser_cli_defaults_to_employer_catalog():
    result = subprocess.run(
        [sys.executable, "-m", "parser_mvp", "single", "--text", "Цена T03"],
        capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["product_id"] == "T03"
    assert payload["availability"] == "out_of_stock"
    assert payload["price"] == "6490"


def test_batch_checks_extended_contract_and_exports_stock(tmp_path):
    summary = run_batch(
        "examples/store_core/catalog_questions.jsonl",
        load_catalog(DEFAULT_CATALOG_PATH), tmp_path,
    )
    assert (summary["PASS"], summary["FAIL"], summary["UNREVIEWED"]) == (10, 0, 0)
    with (tmp_path / "results.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter=";"))
    assert rows[3]["price"] == "6490"
    assert rows[3]["available_sizes"] == "[]"
    assert rows[3]["availability"] == "out_of_stock"
