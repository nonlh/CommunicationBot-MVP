"""Conservative anchored whitelist, not a dictionary of all unknown requests."""
import re

from .normalization import normalize

RULES = (
    ("PRICE", r"(?:сколько стоит|сколько стоят|какая цена|какая стоимость|по какой цене|цен[ау]|стоимость)(?: на)?(?: (.*))?"),
    ("SIZES", r"(?:какие (?:есть |доступны )?размеры|каких размеров|размеры)(?: (.*))?"),
    ("AVAILABILITY", r"(?:есть ли|есть|в наличии|доступен ли|доступна ли)(?: (.*))?"),
    ("AVAILABILITY", r"(.+?) (?:есть в наличии|в наличии|есть)"),
    ("AVAILABILITY", r"(?:мне )?нуж(?:ен|на|но|ны) (.+)"),
    ("AVAILABILITY", r"(.+?) можно (?:в )?(размер(?:а|е|ом|у)? \w+)"),
    ("ORDER", r"(?:хочу|хотел бы|хотела бы|можно) (?:заказать|купить|оформить заказ)(?: на)?(?: (.*))?"),
    ("ORDER", r"хочу (?!заказать\b|купить\b|оформить\b|посмотреть\b)(.+)"),
    ("ORDER", r"(.+?) (?:хочу|хотел бы|хотела бы|можно) (?:заказать|купить|оформить заказ)"),
    ("CATALOG", r"(?:(?:покажи|покажите|пришли|пришлите|можно|хочу посмотреть) )?каталог"),
)


def recognize(text: str) -> tuple[str, str, str]:
    normalized = normalize(text)
    normalized = re.sub(r"^(?:(?:привет|здравствуйте|добрый день|добрый вечер|подскажите|скажите|пожалуйста)\s+)+", "", normalized)
    normalized = re.sub(r"^а\s+", "", normalized)
    normalized = re.sub(r"(?:\s+(?:пожалуйста|спасибо|(?:очень )?срочно))+$", "", normalized)
    if re.search(r"\b(?:не|нет|без|и|или)\b", normalized):
        return "UNKNOWN", "", "Отрицание, несколько объектов или неоднозначная связка требуют уточнения"
    if re.search(r"\bдоставк\w*\b", normalized):
        return "UNKNOWN", "", "Запрос о доставке не является поддерживаемым вопросом о цене товара"
    if re.search(r"\b(?:оплат|платеж|возврат|материал|состав|ткан)\w*\b", normalized):
        return "UNKNOWN", "", "Эти сведения отсутствуют в каталоге; требуется ответ человека"
    # Keep unsupported second questions out of an otherwise valid product query.
    questions = [normalize(part) for part in text.split("?") if normalize(part)]
    if len(questions) > 1 and any(part not in ("спасибо", "пожалуйста") for part in questions[1:]):
        return "UNKNOWN", "", "Несколько вопросов требуют раздельной обработки"
    hits = []
    for intent, pattern in RULES:
        match = re.fullmatch(pattern, normalized)
        if match:
            query = " ".join(group for group in match.groups() if group)
            query = re.sub(r"^(?:у вас |у )", "", query)
            if re.search(r"\b(?:какие|какая|какой|сколько|почему|зачем|когда|из чего|из какого|размеры|наличие|каталог)\b", query):
                return "UNKNOWN", "", "Дополнительный вопрос выходит за поддерживаемый тип запроса"
            hits.append((intent, query, pattern))
    if not hits or len({h[0] for h in hits}) != 1:
        return "UNKNOWN", "", "Безопасный поддерживаемый тип запроса не распознан"
    return hits[0]
