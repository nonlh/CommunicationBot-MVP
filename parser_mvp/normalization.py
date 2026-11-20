"""Small explicit inflection vocabulary; negations and modifiers are retained."""
import re
import unicodedata

FORMS = {
    "футболки": "футболка", "футболку": "футболка", "футболке": "футболка",
    "рубашки": "рубашка", "рубашку": "рубашка", "рубашке": "рубашка",
    "толстовки": "толстовка", "толстовку": "толстовка",
    "платья": "платье", "платью": "платье",
    "джинсов": "джинсы", "джинсам": "джинсы", "джинсах": "джинсы",
    "джинсами": "джинсы",
}
COLORS = {
    "синяя": ("синий", "синюю", "синей", "синего", "синее", "синие", "синих", "синими"),
    "белая": ("белый", "белую", "белой", "белого", "белое"),
    "черная": ("черный", "черную", "черной", "черного", "черное"),
    "зеленая": ("зеленый", "зеленую", "зеленой", "зеленого", "зеленое"),
    "красная": ("красный", "красную", "красной", "красного", "красное"),
    "бежевая": ("бежевый", "бежевую", "бежевой", "бежевого", "бежевое"),
    "серая": ("серый", "серую", "серой", "серого", "серое"),
}
FORMS.update({form: base for base, forms in COLORS.items() for form in forms})
for _base in ("белая", "черная", "зеленая", "красная", "бежевая", "серая"):
    _stem = _base[:-2]
    COLORS[_base] += tuple(_stem + suffix for suffix in ("ые", "ых", "ыми", "ым"))
    FORMS.update({form: _base for form in COLORS[_base]})


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def product_key(text: str) -> str:
    return " ".join(FORMS.get(word, word) for word in normalize(text).split())
