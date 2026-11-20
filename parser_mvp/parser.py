"""Pure parsing: no writes, orders, notifications or retained dialogue state."""
import re
from collections.abc import Mapping

from .matching import match_product
from .models import Catalog, MatchConfig
from .normalization import normalize
from .rules import recognize


def _size(query: str, catalog: Catalog) -> tuple[str, str | None, str, str | None, str]:
    known = {normalize(size): size for product in catalog.products for size in product.sizes}
    explicit = list(re.finditer(r"\b(?:в\s+)?размер(?:а|е|ом|у)?\s+([\w]+)\b", query))
    tokens = {"xxxs", "xxxl", "xxs", "xxl", "xs", "xl", "s", "m", "l"}
    tokens.update(key for key in known if not key.isdigit())
    alternatives = "|".join(re.escape(token) for token in sorted(tokens, key=len, reverse=True))
    bare = list(re.finditer(r"\b(?:" + alternatives + r")\b", query))
    matches = explicit + [m for m in bare if not any(e.start() <= m.start() < e.end() for e in explicit)]
    requests = {m.group(1) if m.lastindex else m[0] for m in matches}
    cleaned = query
    for match in sorted(matches, key=lambda m: m.start(), reverse=True):
        cleaned = cleaned[:match.start()] + " " + cleaned[match.end():]
    if len(requests) > 1:
        return normalize(cleaned), None, "Указано несколько размеров; нужен один размер", "|".join(sorted(requests)), "unrecognized"
    if requests:
        requested = requests.pop()
        if requested not in known:
            return normalize(cleaned), None, f"Размер {requested.upper()} неизвестен каталогу", requested.upper(), "unrecognized"
        return normalize(cleaned), known[requested], "", known[requested], "recognized"
    return normalize(cleaned), None, "", None, "absent"


def parse_message(text: str, catalog: Catalog, state=None, *, config: MatchConfig | None = None) -> dict:
    if not isinstance(text, str):
        raise TypeError("text должен быть строкой")
    catalog = Catalog(catalog.visible_products)
    result = {"intent": "UNKNOWN", "product_id": None, "size": None,
              "match_status": "not_applicable", "needs_clarification": True,
              "reason": "", "normalized_text": normalize(text), "candidates": [], "matched_rules": [],
              "requested_size": None, "size_status": "absent",
              "price": None, "currency": None, "available_sizes": None,
              "availability": "unknown"}
    if state is None or state == "general":
        context = {}
    elif isinstance(state, Mapping) and state.get("mode", "general") == "general":
        context = state
    else:
        result["reason"] = "Поддерживается общий режим; FSM в этой версии не реализована"
        return result
    intent, query, rule = recognize(text)
    result["intent"] = intent
    if intent == "UNKNOWN":
        result["reason"] = rule
        return result
    result["matched_rules"] = [rule]
    if intent == "CATALOG":
        result.update(needs_clarification=False, reason="Распознан запрос каталога; конкретный товар не требуется")
        return result
    query, size, size_issue, requested_size, size_status = _size(query, catalog)
    result.update(requested_size=requested_size, size_status=size_status)
    result["size"] = size
    if not query and context.get("product_id"):
        product = next((p for p in catalog.products if p.product_id == context["product_id"]), None)
        if product is None:
            result["reason"] = "Товар из контекста отсутствует в актуальном каталоге"
            return result
        query = product.name
        result["matched_rules"].append("explicit_product_context")
    result.update(match_product(query, catalog, config or MatchConfig()))
    reasons = {
        "not_applicable": "Не указан конкретный товар",
        "not_found": "Указанный товар не найден в актуальном каталоге",
        "possible": "Найден вероятный кандидат; требуется подтверждение товара",
        "ambiguous": "Найдено несколько возможных товаров; требуется уточнение",
        "exact": "Товар однозначно определён по названию или подтверждённому алиасу",
    }
    result["reason"] = reasons[result["match_status"]]
    if size_issue:
        result["reason"] += "; " + size_issue
    product = next((p for p in catalog.products if p.product_id == result["product_id"]), None)
    if product:
        result.update(
            price=str(product.price) if product.price is not None else None,
            currency=product.currency,
            available_sizes=list(product.sizes) if product.stock_known else None,
            availability=product.availability,
        )
    if product and size and normalize(size) not in {normalize(s) for s in product.sizes}:
        result["size_status"] = "unavailable"
        size_issue = (
            "Этого размера нет в наличии по каталогу"
            if product.stock_known else
            "Размер не указан для этого товара в каталоге; наличие не определено"
        )
        result["reason"] += "; " + size_issue
    elif product and requested_size and size_status == "unrecognized" and re.fullmatch(
        r"(?:\d{1,3}|x{0,3}[sl]|m)", requested_size.casefold()
    ):
        result["size_status"] = "unavailable"
    result["needs_clarification"] = result["match_status"] != "exact" or bool(size_issue)
    return result
