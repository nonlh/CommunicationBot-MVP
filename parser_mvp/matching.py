"""Exact full product phrases first; fuzzy scores never confirm a product."""
from rapidfuzz.fuzz import ratio

from .models import Catalog, MatchConfig
from .normalization import COLORS, normalize, product_key

COLOR_FEATURES = {
    form: base for base, forms in COLORS.items() for form in (base, *forms)
}


def _catalog_features(catalog: Catalog) -> tuple[
    dict[str, str], dict[str, str], dict[str, tuple[set[str], set[str]]],
]:
    """Build joint attributes from active records, including their trusted aliases."""
    colors = dict(COLOR_FEATURES)  # Language forms, not a list of stocked colors.
    endings = (
        ("ый", "ая", "ое", "ую", "ые", "ых", "ыми", "ого", "ому", "ой"),
        ("ий", "яя", "ее", "юю", "ие", "их", "ими", "его", "ему", "ей"),
    )
    for product in catalog.visible_products:
        for word in normalize(product.color + " " + product.name).split():
            if word in colors:
                continue
            for forms in endings:
                ending = next((suffix for suffix in forms if word.endswith(suffix)), None)
                if ending and len(word) > len(ending) + 2:
                    stem = word[:-len(ending)]
                    colors.update({stem + suffix: word for suffix in forms})
                    break
        for word in product_key(product.color).split():
            colors.setdefault(word, word)
    attributes = {}
    types = {}
    for product in catalog.visible_products:
        words = set(product_key(" ".join((product.name, *product.aliases))).split())
        actual_colors = {colors[w] for w in words if w in colors}
        actual_colors.update(colors[w] for w in product_key(product.color).split())
        actual_types = words - colors.keys()
        types.update({word: word for word in actual_types})
        attributes[product.product_id] = actual_colors, actual_types
    for word in tuple(types):
        if word.endswith("а") and len(word) > 2:
            for ending in ("у", "е"):
                types.setdefault(word[:-1] + ending, word)
    return colors, types, attributes


def _requested_features(
    words: set[str], vocabulary: dict[str, str], threshold: float,
) -> set[str]:
    """Recognize attributes for filtering; never rewrite the scored product phrase."""
    explicit = {vocabulary[word] for word in words & vocabulary.keys()}
    if explicit:
        return explicit
    inferred = set()
    for word in words:
        by_feature: dict[str, float] = {}
        for form, feature in vocabulary.items():
            by_feature[feature] = max(by_feature.get(feature, 0), ratio(word, form))
        scores = sorted(((score, value) for value, score in by_feature.items()), reverse=True)
        if scores and scores[0][0] >= threshold and (len(scores) == 1 or scores[0][0] - scores[1][0] >= 8):
            inferred.add(scores[0][1])
    return inferred


def _compatible(attributes: tuple[set[str], set[str]], colors: set[str], types: set[str]) -> bool:
    actual_colors, actual_types = attributes
    return (
        colors <= actual_colors and types <= actual_types
    )


def match_product(query: str, catalog: Catalog, config: MatchConfig) -> dict:
    key = product_key(query)
    if not key:
        return {"match_status": "not_applicable", "product_id": None, "candidates": []}
    words = set(key.split())
    color_forms, type_forms, attributes = _catalog_features(catalog)
    colors = _requested_features(words, color_forms, 80)
    types = _requested_features(words - color_forms.keys(), type_forms, 80)
    products = tuple(
        p for p in catalog.visible_products
        if _compatible(attributes[p.product_id], colors, types)
    )

    def phrase(value: str) -> str:
        return " ".join(
            color_forms.get(w, type_forms.get(w, w)) for w in product_key(value).split()
        )

    phrase_key = phrase(query)
    for field in ("name", "aliases"):
        hits = []
        for product in products:
            variants = (product.name,) if field == "name" else product.aliases
            if any(phrase(v) == phrase_key for v in variants):
                hits.append({"product_id": product.product_id, "name": product.name, "score": 100.0, "via": field})
        if hits:
            return {"match_status": "exact" if len(hits) == 1 else "ambiguous",
                    "product_id": hits[0]["product_id"] if len(hits) == 1 else None, "candidates": hits}
    # A shared color or category cannot conceal a different, unrecognized modifier.
    if any(
        not _requested_features({word}, color_forms, 80)
        and not _requested_features({word}, type_forms, 80)
        for word in words
    ):
        return {"match_status": "not_found", "product_id": None, "candidates": []}
    candidates = []
    for product in products:
        variants = [product_key(product.name), *(product_key(v) for v in product.aliases)]
        score = max(ratio(key, variant) for variant in variants)
        partial = any(set(key.split()) < set(variant.split()) for variant in variants)
        if score >= config.fuzzy_threshold or (len(key) >= 3 and partial):
            candidates.append({"product_id": product.product_id, "name": product.name,
                               "score": round(score, 2), "via": "partial" if partial else "fuzzy"})
    candidates.sort(key=lambda c: (-c["score"], c["product_id"]))
    if not candidates:
        status = "not_found"
    elif len(candidates) > 1 and (candidates[0]["via"] == "partial" or candidates[0]["score"] - candidates[1]["score"] <= config.ambiguity_gap):
        status = "ambiguous"
    else:
        status = "possible"
    return {"match_status": status, "product_id": None, "candidates": candidates}
