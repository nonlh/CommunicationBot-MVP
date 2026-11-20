"""Independent, strict CSV/XLSX catalog loader; never supplies missing facts."""
import csv
import io
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from .models import Catalog, Product
from .normalization import normalize

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[1] / "catalog" / "mvp_catalog.csv"

HEADERS = {
    "product_id": "product_id", "id": "product_id", "артикул": "product_id",
    "name": "name", "название": "name", "наименование": "name",
    "aliases": "aliases", "алиасы": "aliases",
    "sizes": "sizes", "размеры": "sizes", "color": "color", "цвет": "color",
    "price": "price", "цена": "price", "currency": "currency",
    "валюта": "currency", "active": "active", "активен": "active",
    "price_rub": "price", "цена руб": "price", "цена, руб.": "price",
    "цена (руб.)": "price", "price_usd": "price", "price_eur": "price",
    "sizes_in_stock": "sizes", "размеры в наличии": "sizes",
}
HEADER_CURRENCY = {
    "price_rub": "RUB", "цена руб": "RUB", "цена, руб.": "RUB",
    "цена (руб.)": "RUB", "price_usd": "USD", "price_eur": "EUR",
}


def _columns(header: list) -> tuple[list[str | None], str | None, bool]:
    keys = [str(value or "").strip().casefold() for value in header]
    columns = [HEADERS.get(key) for key in keys]
    if not {"product_id", "name"} <= set(columns):
        raise ValueError("Не найдены однозначные колонки product_id и name")
    recognized = [c for c in columns if c]
    if len(recognized) != len(set(recognized)):
        raise ValueError("В каталоге повторяются колонки или их синонимы")
    currency = next((HEADER_CURRENCY[key] for key in keys if key in HEADER_CURRENCY), None)
    stock_known = any(key in ("sizes_in_stock", "размеры в наличии") for key in keys)
    return columns, currency, stock_known


def _product(
    fields: dict[str, str], header_currency: str | None, stock_known: bool,
) -> Product:
    price_text = fields.get("price", "")
    price = None
    if price_text:
        if not re.fullmatch(r"\d+(?:[.,]\d+)?", price_text):
            raise ValueError("Цена должна быть числом без символа валюты и разделителей тысяч")
        try:
            price = Decimal(price_text.replace(",", "."))
        except InvalidOperation as exc:
            raise ValueError("Некорректная цена") from exc
    currency = fields.get("currency", "").upper() or header_currency
    if header_currency and currency != header_currency:
        raise ValueError("Валюта строки противоречит заголовку цены")
    active_text = fields.get("active", "").casefold()
    if active_text not in ("", "1", "0", "true", "false", "да", "нет"):
        raise ValueError("active: допустимы 1/0, true/false, да/нет")
    aliases = tuple(v.strip() for v in fields.get("aliases", "").split("|") if v.strip())
    sizes = tuple(v.strip() for v in fields.get("sizes", "").split("|") if v.strip())
    if len({normalize(size) for size in sizes}) != len(sizes):
        raise ValueError("Размеры товара повторяются после нормализации")
    return Product(
        fields["product_id"], fields["name"], aliases, sizes,
        fields.get("color", ""), price, currency,
        active_text not in ("0", "false", "нет"),
        stock_known,
    )


def load_catalog(path: str | Path) -> Catalog:
    """Read one UTF-8 CSV or one-sheet XLSX; reject ambiguous headers/formulas."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"Файл каталога не найден: {path}")
    if path.suffix.casefold() == ".csv":
        text = path.read_text(encoding="utf-8-sig")
        candidates = []
        for delimiter in (";", ","):
            try:
                rows = list(csv.reader(
                    io.StringIO(text, newline=""), delimiter=delimiter, strict=True,
                ))
                columns, currency, stock_known = _columns(rows[0] if rows else [])
            except (ValueError, csv.Error):
                continue
            candidates.append((rows, columns, currency, stock_known))
        if len(candidates) != 1:
            raise ValueError("Не найдены однозначные колонки product_id и name; проверьте дубли заголовков")
        rows, columns, currency, stock_known = candidates[0]
    elif path.suffix.casefold() == ".xlsx":
        from openpyxl import load_workbook
        from openpyxl.utils.exceptions import InvalidFileException

        try:
            workbook = load_workbook(
                path, read_only=True, data_only=False, keep_links=False,
            )
        except (BadZipFile, InvalidFileException, KeyError, ParseError) as exc:
            raise ValueError("Не удалось прочитать XLSX; проверьте формат файла") from exc
        try:
            if len(workbook.worksheets) != 1:
                raise ValueError("XLSX должен содержать ровно один лист каталога")
            rows = list(workbook.worksheets[0].iter_rows(values_only=True))
            columns, currency, stock_known = _columns(list(rows[0]) if rows else [])
        except (BadZipFile, KeyError, ParseError) as exc:
            raise ValueError("Повреждены данные листа XLSX") from exc
        finally:
            workbook.close()
    else:
        raise ValueError("Каталог должен быть CSV UTF-8 или XLSX")
    products = []
    for number, row in enumerate(rows[1:], start=2):
        if not row or all(value is None or not str(value).strip() for value in row):
            continue
        if len(row) != len(columns):
            raise ValueError(f"Неверное число полей каталога в строке {number}")
        fields = {
            key: str(value).strip() if value is not None else ""
            for key, value in zip(columns, row) if key
        }
        if any(value.startswith("=") for value in fields.values()):
            raise ValueError(f"Строка {number}: формулы не поддерживаются; сохраните значения")
        try:
            products.append(_product(fields, currency, stock_known))
        except ValueError as exc:
            raise ValueError(f"Строка {number}: {exc}") from exc
    return Catalog(tuple(products))
