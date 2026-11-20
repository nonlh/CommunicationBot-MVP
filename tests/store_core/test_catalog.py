from decimal import Decimal

import pytest
from openpyxl import Workbook

from parser_mvp import load_catalog, parse_message


def write_csv(tmp_path, content):
    path = tmp_path / "catalog.csv"
    path.write_text(content, encoding="utf-8-sig")
    return path


def test_price_decimal_and_explicit_currency(tmp_path):
    catalog = load_catalog(write_csv(tmp_path, "id;name;price;currency\nA;Кепка;0,10;\nB;Шарф;120.25;EUR\n"))
    assert catalog.products[0].price == Decimal("0.10")
    assert catalog.products[0].currency is None
    assert catalog.products[1].currency == "EUR"
    assert catalog.products[0].price + Decimal("0.20") == Decimal("0.30")


def test_currency_from_header_and_conflict(tmp_path):
    assert load_catalog(write_csv(tmp_path, "id;name;цена (руб.)\nA;Кепка;100\n")).products[0].currency == "RUB"
    with pytest.raises(ValueError, match="противоречит"):
        load_catalog(write_csv(tmp_path, "id;name;price_rub;currency\nA;Кепка;100;USD\n"))


@pytest.mark.parametrize("header,row", [
    ("id;name;название", "A;Кепка;Шарф"),
    ("id;name;price;цена", "A;Кепка;1;2"),
    ("id;товар", "A;Кепка"),
    ("id;name;active", "A;Кепка;maybe"),
    ("id;name;price", "A;Кепка;-1"),
    ("id;name;price", "A;Кепка;NaN"),
    ("id;name;price", "A;Кепка;1 200"),
    ("id;name;price", "A;Кепка;=1+2"),
    ("id;name;sizes", "A;Кепка;M|m"),
    ("id;name", "A;Кепка;extra"),
])
def test_invalid_catalog_is_diagnostic(tmp_path, header, row):
    with pytest.raises(ValueError):
        load_catalog(write_csv(tmp_path, header + "\n" + row + "\n"))


def test_disabled_not_matched(tmp_path):
    catalog = load_catalog(write_csv(tmp_path, "id;name;active\nA;Кепка;0\nB;Шарф;1\n"))
    assert len(catalog.products) == 2
    assert len(catalog.visible_products) == 1
    assert parse_message("Какая цена кепка", catalog)["product_id"] is None


def test_xlsx_values_and_optional_cells(tmp_path):
    path = tmp_path / "catalog.xlsx"
    wb = Workbook()
    wb.active.append(["артикул", "название", "price_usd", "sizes", "active"])
    wb.active.append(["A", "Кепка", 12.25, "M|L", True])
    wb.active.append(["B", "Шарф", None, None, False])
    wb.save(path)
    wb.close()
    catalog = load_catalog(path)
    assert catalog.products[0].price == Decimal("12.25")
    assert catalog.products[0].currency == "USD"
    assert catalog.products[1].price is None
    assert not catalog.products[1].active


@pytest.mark.parametrize("case", ["formula", "multiple_sheets", "duplicate_headers"])
def test_unsafe_xlsx_rejected(tmp_path, case):
    path = tmp_path / "catalog.xlsx"
    wb = Workbook()
    wb.active.append(["id", "name", "price"] if case != "duplicate_headers" else ["id", "name", "название"])
    wb.active.append(["A", "Кепка", "=1+2" if case == "formula" else "100"])
    if case == "multiple_sheets":
        wb.create_sheet("Another catalog")
    wb.save(path)
    wb.close()
    with pytest.raises(ValueError):
        load_catalog(path)


@pytest.mark.parametrize("text,status,requested", [
    ("Есть футболка синяя", "absent", None),
    ("Есть футболка синяя M", "recognized", "M"),
    ("Есть футболка синяя XL", "unavailable", "XL"),
    ("Есть футболка синяя размер 43", "unavailable", "43"),
    ("Есть футболка синяя размер Z9", "unrecognized", "Z9"),
    ("Есть футболка синяя M L", "unrecognized", "l|m"),
])
def test_structured_size_status(text, status, requested):
    catalog = load_catalog("tests/fixtures/store_catalog.csv")
    result = parse_message(text, catalog)
    assert result["size_status"] == status
    assert result["requested_size"] == requested


def test_delivery_stays_unknown():
    catalog = load_catalog("tests/fixtures/store_catalog.csv")
    assert parse_message("Сколько стоит доставка футболки?", catalog)["intent"] == "UNKNOWN"


def test_corrupt_csv_and_xlsx_produce_validation_errors(tmp_path):
    with pytest.raises(ValueError):
        load_catalog(write_csv(tmp_path, 'id;name\nA;"Unclosed quote'))
    path = tmp_path / "broken.xlsx"
    path.write_text("not an Excel ZIP", encoding="utf-8")
    with pytest.raises(ValueError, match="XLSX"):
        load_catalog(path)
