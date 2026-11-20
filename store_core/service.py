"""Conservative persistent FSM shared by Telegram and the offline simulator."""
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from uuid import uuid4

from parser_mvp import Catalog, load_catalog, parse_message
from parser_mvp.models import Product
from parser_mvp.normalization import normalize

from .storage import STATUSES, Store, utc_now

ORDER_CONFIRMATION = (
    "Спасибо! Ваша заявка зарегистрирована. "
    "Ответственный свяжется с вами, чтобы уточнить детали."
)
HUMAN_CONFIRMATION = (
    "Ваше обращение зарегистрировано и передано ответственному. "
    "Спасибо! С вами свяжутся по указанному контакту."
)
REPEAT_CONFIRMATION = "Ваше обращение уже зарегистрировано. Спасибо!"
CUSTOMER_HOME_BUTTONS = (
    ("Новое обращение", "ui:start"), ("Каталог", "ui:catalog"),
)
NOTIFICATION_LABELS = {
    "sent": "Отправлено", "failed": "Ошибка отправки", "pending": "Ожидает отправки",
}


@dataclass
class Reply:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)
    application_id: int | None = None

    def __post_init__(self) -> None:
        # Cached JSON stores button pairs as lists; keep the public view uniform.
        self.buttons = [tuple(button) for button in self.buttons]


def product_text(product: Product) -> str:
    price = "не указана" if product.price is None else str(product.price)
    if product.currency:
        price += " " + product.currency
    elif product.price is not None:
        price += " (валюта не указана)"
    if product.availability == "out_of_stock":
        return f"{product.name}\nЦена: {price}\nСейчас товара нет в наличии."
    if product.stock_known:
        sizes = ", ".join(product.sizes) or "нет"
        availability = "в наличии" if product.sizes else "нет в наличии"
        return (
            f"{product.name}\nЦена: {price}\nРазмеры в наличии: {sizes}\n"
            f"Наличие по каталогу: {availability}"
        )
    sizes = ", ".join(product.sizes) or "не указаны"
    return f"{product.name}\nЦена: {price}\nРазмеры по каталогу: {sizes}"


class Shop:
    def __init__(
        self, catalog: Catalog, store: Store, admin_id: int | None = None,
    ) -> None:
        self.catalog = catalog
        self.store = store
        self.admin_id = admin_id

    def product(self, product_id: str | None) -> Product | None:
        return next(
            (p for p in self.catalog.visible_products if p.product_id == product_id),
            None,
        )

    @staticmethod
    def _new(session: dict) -> None:
        session.clear()
        session.update(operation=uuid4().hex, stage="product")

    @staticmethod
    def _button(session: dict, label: str, action: str) -> tuple[str, str]:
        token = uuid4().hex[:12]
        session.setdefault("buttons", {})[token] = action
        return label, f"{session['operation']}:{token}"

    def _navigation(self, session: dict, reply: Reply) -> Reply:
        if session.get("stage") in ("done", "cancelled"):
            if not reply.buttons:
                reply.buttons += list(CUSTOMER_HOME_BUTTONS)
        else:
            reply.buttons += [
                self._button(session, "Назад", "back"),
                self._button(session, "Отмена", "cancel"),
            ]
        return reply

    @staticmethod
    def _home_reply(text: str, application_id: int | None = None) -> Reply:
        return Reply(text, list(CUSTOMER_HOME_BUTTONS), application_id)

    @staticmethod
    def _is_greeting(text: str) -> bool:
        return normalize(text) in (
            "здравствуйте", "здравствуй", "привет", "добрый день",
            "добрый вечер", "доброе утро", "доброго времени суток",
        )

    def _continue(self, session: dict, text: str) -> Reply:
        """Redisplay current controls without clearing the form or valid buttons."""
        stage = session["stage"]
        prompts = {
            "size": "Выберите размер, чтобы продолжить текущее оформление.",
            "name": "Введите имя, чтобы продолжить текущее оформление.",
            "contact": "Введите контакт, чтобы продолжить текущее оформление.",
            "confirm": "Проверьте данные в предыдущем сообщении и подтвердите отправку.",
            "human_offer": "Можно передать ваш вопрос ответственному.",
        }
        buttons = []
        if stage == "size":
            buttons = [
                self._button(session, size, f"s{index}")
                for index, size in enumerate(session.get("size_choices", []))
            ]
        elif stage == "confirm":
            buttons = [self._button(session, "Подтвердить", "confirm")]
        elif stage == "human_offer":
            buttons = [self._button(session, "Оставить вопрос человеку", "human")]
        reply = Reply(text + "\n" + prompts.get(stage, "Выберите действие ниже."), buttons)
        self._navigation(session, reply)
        reply.buttons += list(CUSTOMER_HOME_BUTTONS)
        return reply

    def _information(self, session: dict, text: str, result: dict) -> Reply:
        """Answer from the catalog; entering checkout requires a separate action."""
        product = self.product(result.get("product_id"))
        active = session["stage"] not in ("product", "done", "cancelled")
        if product is None:
            if result["match_status"] == "not_found":
                message = "Такого товара нет в каталоге."
                if active:
                    return self._continue(session, message)
                return Reply(message, [("Каталог", "ui:catalog")])
            if active:
                return self._continue(session, "Не удалось однозначно определить товар в вопросе.")
            if session["stage"] in ("done", "cancelled"):
                self._new(session)
            return self._free_text(session, text, result=result)
        if product.availability == "out_of_stock":
            return Reply(product_text(product), [("Каталог", "ui:catalog")])
        intent = result["intent"]
        if intent == "PRICE":
            price = "не указана" if product.price is None else str(product.price)
            if product.currency:
                price += " " + product.currency
            elif product.price is not None:
                price += " (валюта не указана)"
            body = f"{product.name}\nЦена: {price}"
            if product.price is None:
                body += "\nЦена не указана. Требуется ответ человека."
        elif intent == "SIZES":
            label = "Размеры в наличии" if product.stock_known else "Размеры по каталогу"
            empty = "нет" if product.stock_known else "не указаны"
            body = f"{product.name}\n{label}: {', '.join(product.sizes) or empty}"
        else:
            body = product_text(product)
        requested = result.get("requested_size")
        if requested and "|" in requested:
            body += "\nУточните, пожалуйста, один размер."
        elif requested and normalize(requested) not in {normalize(s) for s in product.sizes}:
            body += (
                "\nЗапрошенный размер отсутствует в наличии по каталогу."
                if product.stock_known else "\nЭтот размер не указан для товара."
            )
        if intent == "AVAILABILITY" and not product.stock_known:
            body += "\nКаталог не содержит складских остатков; наличие уточнит человек."
        if active:
            return self._continue(session, body)
        # Browsing context is separate from the completed operation and its proof.
        session.update(info_product_id=product.product_id, info_question=text)
        if session["stage"] == "product":
            session.update(product_id=product.product_id, product_name=product.name)
        buttons = []
        if product.sizes:
            buttons.append(self._button(session, "Оформить заявку", f"order:{product.product_id}"))
        if not product.sizes or (intent == "PRICE" and product.price is None) or not product.stock_known:
            buttons.append(self._button(session, "Оставить вопрос человеку", "human"))
        return Reply(body, buttons + [("Каталог", "ui:catalog")])

    def _catalog(self, session: dict, page: int = 0) -> Reply:
        products = self.catalog.visible_products
        session.update(stage="product", choices=[p.product_id for p in products])
        page = max(0, min(page, max(0, (len(products) - 1) // 10)))
        buttons = [
            self._button(session, product.name[:100], f"p{index}")
            for index, product in enumerate(products)
            if page * 10 <= index < (page + 1) * 10
        ]
        if page:
            buttons.append(self._button(
                session, "Предыдущие товары", f"page{page - 1}",
            ))
        if (page + 1) * 10 < len(products):
            buttons.append(self._button(
                session, "Следующие товары", f"page{page + 1}",
            ))
        return Reply(
            "Выберите товар из каталога." if products
            else "В каталоге нет доступных товаров.",
            buttons,
        )

    def _sizes(self, session: dict) -> Reply:
        product = self.product(session.get("product_id"))
        if product is None:
            return self._catalog(session)
        if product.availability == "out_of_stock":
            session["stage"] = "product"
            return Reply(product_text(product), [("Каталог", "ui:catalog")])
        session.update(stage="size", size_choices=list(product.sizes))
        buttons = [
            self._button(session, size[:100], f"s{index}")
            for index, size in enumerate(product.sizes)
        ]
        session.setdefault("question", f"Уточнить наличие и размеры: {product.name}")
        buttons.append(self._button(session, "Уточнить у человека", "human"))
        if not product.sizes:
            session["stage"] = "human_offer"
            buttons = [self._button(session, "Оставить вопрос человеку", "human")]
            return Reply(
                product_text(product)
                + "\nДля оформления нужно уточнить доступный размер. "
                "Можно оставить вопрос ответственному.",
                buttons,
            )
        prompt = (
            "\nВыберите размер из наличия по каталогу."
            if product.stock_known else
            "\nВыберите размер. Остатки требуют подтверждения сотрудником."
        )
        return Reply(
            product_text(product) + prompt,
            buttons,
        )

    def _preview(self, session: dict) -> Reply:
        session["stage"] = "confirm"
        subject = session.get("question") if session.get("human") else (
            f"{session.get('product_name')}, размер {session.get('size')}"
        )
        title = "Проверьте обращение" if session.get("human") else "Проверьте заявку"
        return Reply(
            f"{title}:\n{subject}\nИмя: {session['name']}\nКонтакт: {session['contact']}\n"
            "Всё верно? Нажмите «Подтвердить», чтобы отправить.",
            [self._button(session, "Подтвердить", "confirm")],
        )

    def handle(
        self, user_id: int, event_id: str, text: str = "", *,
        callback: str | None = None,
    ) -> Reply:
        """One atomic transaction: application + FSM + replay receipt."""
        if not event_id or len(event_id) > 200:
            raise ValueError("Нужен устойчивый ID события")
        with self.store.transaction() as db:
            previous = db.execute(
                "SELECT * FROM events WHERE event_id=?", (event_id,),
            ).fetchone()
            if previous:
                if previous["user_id"] != user_id:
                    return Reply("Не удалось открыть это сообщение. Пожалуйста, попробуйте ещё раз.")
                return Reply(**json.loads(previous["response"]))
            session = self.store.session(db, user_id)
            reply = self._handle(db, session, user_id, event_id, text.strip(), callback)
            self.store.save_session(db, user_id, session)
            db.execute("INSERT INTO events VALUES (?, ?, ?, ?)", (
                event_id, user_id, json.dumps(asdict(reply), ensure_ascii=False), utc_now(),
            ))
            return reply

    def _handle(
        self, db: sqlite3.Connection, session: dict, user_id: int,
        event_id: str, text: str, callback: str | None,
    ) -> Reply:
        if text.startswith("/admin") or (callback and callback.startswith("admin:")):
            return self._admin(db, user_id, text, callback)
        if text.split("@")[0] in ("/start", "/catalog"):
            self._new(session)
            return self._navigation(session, self._catalog(session))
        if not session:
            self._new(session)
        action = None
        if callback:
            operation, separator, token = callback.partition(":")
            if not separator or operation != session.get("operation"):
                return self._home_reply("Кнопка устарела. Выберите действие ниже, чтобы продолжить.")
            if session["stage"] == "done" and callback == session.get("confirmed_callback"):
                return self._home_reply(REPEAT_CONFIRMATION, session.get("application_id"))
            action = session.get("buttons", {}).get(token)
            if action is None:
                return self._home_reply("Кнопка устарела. Выберите действие ниже, чтобы продолжить.")
        if callback is None and self._is_greeting(text):
            if session["stage"] in ("product", "done", "cancelled"):
                return self._home_reply("Здравствуйте! Посмотрите каталог или задайте вопрос о товаре.")
            return self._continue(session, "Здравствуйте!")
        if callback is None:
            result = parse_message(
                text, self.catalog,
                state={"product_id": session.get("product_id") or session.get("info_product_id")},
            )
            if result["intent"] in ("PRICE", "SIZES", "AVAILABILITY"):
                return self._information(session, text, result)
        session["buttons"] = {}
        if text == "/cancel" or action == "cancel":
            session.clear()
            session.update(stage="cancelled", operation=uuid4().hex)
            return self._home_reply("Оформление отменено. Вы можете выбрать товар или оставить новое обращение.")
        if text == "/back" or action == "back":
            return self._navigation(session, self._back(session))
        if action and action.startswith("order:"):
            product = self.product(action[len("order:"):])
            if product is None:
                return self._home_reply("Товар больше недоступен. Выберите другой товар в каталоге.")
            if session["stage"] in ("done", "cancelled"):
                self._new(session)
            session.update(product_id=product.product_id, product_name=product.name)
            session.pop("human", None)
            return self._navigation(session, self._sizes(session))
        if action == "human" and session["stage"] in ("product", "done", "cancelled"):
            question = session.get("info_question", "Вопрос о товаре")
            product = self.product(session.get("info_product_id"))
            if session["stage"] in ("done", "cancelled"):
                self._new(session)
            session.update(human=True, stage="name", question=question)
            if product:
                session.update(product_id=product.product_id, product_name=product.name)
            session.pop("size", None)
            return self._navigation(session, Reply("Введите имя для обращения к человеку."))
        if session["stage"] in ("done", "cancelled"):
            # A new message is not a duplicate of a confirmation operation.
            self._new(session)
            return self._navigation(session, self._free_text(session, text))
        stage = session["stage"]
        if stage == "product":
            if action and re.fullmatch(r"page\d+", action):
                reply = self._catalog(session, int(action[4:]))
            elif action and re.fullmatch(r"p\d+", action):
                index = int(action[1:])
                choices = session.get("choices", [])
                if index < len(choices) and self.product(choices[index]):
                    product = self.product(choices[index])
                    session.update(
                        product_id=product.product_id, product_name=product.name,
                    )
                    pending = session.pop("pending_parse", {})
                    if pending.get("intent") == "ORDER":
                        reply = self._sizes(session)
                    else:
                        pending.update(product_id=product.product_id)
                        pending.setdefault("intent", "CATALOG")
                        reply = self._information(session, session.get("question", product.name), pending)
                else:
                    reply = self._home_reply("Товар больше недоступен. Выберите другой товар в каталоге.")
            elif callback:
                reply = Reply("Выберите товар текущими кнопками.")
            else:
                reply = self._free_text(session, text)
        elif stage == "size":
            if action == "human":
                session.update(human=True, stage="name")
                if session.get("pending_question"):
                    session["question"] = session.pop("pending_question")
                session.pop("size", None)
                return self._navigation(
                    session, Reply("Введите имя для обращения к человеку."),
                )
            product = self.product(session.get("product_id"))
            size = None
            explicit_size = bool(action and re.fullmatch(r"s\d+", action))
            if product:
                if action and re.fullmatch(r"s\d+", action):
                    index = int(action[1:])
                    choices = session.get("size_choices", [])
                    size = choices[index] if index < len(choices) else None
                elif callback is None:
                    key = normalize(text)
                    match = re.fullmatch(r"размер\s+(\w+)", key)
                    if match:
                        key = match[1]
                    explicit_size = bool(match) or bool(re.fullmatch(r"(?:x{0,4}[sl]|m|\d{1,3})", key))
                    size = next(
                        (s for s in product.sizes if normalize(s) == key),
                        None,
                    )
                    explicit_size = explicit_size or size is not None
            if product and size in product.sizes:
                session.update(size=size, stage="name")
                reply = Reply("Введите имя покупателя.")
            else:
                if explicit_size:
                    message = (
                        "Этот размер отсутствует в наличии по каталогу."
                        if product and product.stock_known else
                        "Этот размер не указан для товара."
                    )
                else:
                    message = "Не удалось понять сообщение. Вы можете продолжить оформление, открыть каталог или начать заново."
                    if callback is None and self._is_question(text):
                        session["pending_question"] = text
                        reply = self._continue(session, "По этому вопросу требуется ответ человека.")
                        reply.buttons.insert(0, self._button(session, "Оставить вопрос человеку", "human"))
                        return reply
                return self._continue(session, message)
        elif stage == "human_offer":
            if action == "human":
                session.update(human=True, stage="name")
                session.pop("size", None)
                reply = Reply("Введите имя для обращения к человеку.")
            else:
                reply = Reply(
                    "Для передачи вопроса нажмите кнопку или отмените оформление.",
                    [self._button(session, "Оставить вопрос человеку", "human")],
                )
        elif stage == "name":
            valid_name = (
                2 <= len(text) <= 100 and any(c.isalpha() for c in text)
                and all(c.isalpha() or c in " -'’" for c in text)
            )
            if callback is None and valid_name:
                session.update(name=text, stage="contact")
                reply = Reply("Введите телефон (+ и 7–15 цифр), email или Telegram @username.")
            else:
                reply = Reply("Введите имя буквами, от 2 до 100 символов.")
        elif stage == "contact":
            if callback is None and self._valid_contact(text):
                session["contact"] = text
                reply = self._preview(session)
            else:
                reply = Reply("Нужен телефон (+ и 7–15 цифр), email или @username (5–32 символа).")
        elif stage == "confirm":
            if action == "confirm":
                product = self.product(session.get("product_id"))
                invalid_product = (
                    product is None or session.get("size") not in product.sizes
                )
                if not session.get("human") and invalid_product:
                    session.pop("size", None)
                    reply = self._sizes(session) if product else self._catalog(session)
                    reply.text = (
                        "Каталог изменился; проверьте товар и размер заново.\n"
                        + reply.text
                    )
                else:
                    application_id = self.store.create_application(db, user_id, event_id, session)
                    confirmation = HUMAN_CONFIRMATION if session.get("human") else ORDER_CONFIRMATION
                    # Do not retain extra copies of name/contact in a finished session.
                    operation = session["operation"]
                    session.clear()
                    session.update(
                        stage="done", operation=operation,
                        application_id=application_id,
                        confirmed_callback=callback,
                    )
                    reply = Reply(
                        confirmation,
                        application_id=application_id,
                    )
            else:
                reply = self._preview(session)
        else:
            reply = self._home_reply("Выберите действие ниже, чтобы начать оформление.")
        return self._navigation(session, reply)

    @staticmethod
    def _is_question(text: str) -> bool:
        """Only explicit questions may interrupt size entry; bare XL stays a size."""
        return "?" in text or bool(re.search(
            r"\b(?:цен[ау]|размеры|сколько|какие|какая|какой|есть|доставк\w*|"
            r"оплат\w*|возврат\w*|материал\w*|состав\w*|ткан\w*)\b",
            normalize(text),
        ))

    @staticmethod
    def _valid_contact(text: str) -> bool:
        if len(text) > 254:
            return False
        return bool(
            re.fullmatch(r"\+?\d{7,15}", re.sub(r"[ ()-]", "", text))
            or re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", text)
            or re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", text)
        )

    def _free_text(self, session: dict, text: str, *, result: dict | None = None) -> Reply:
        result = result or parse_message(
            text, self.catalog, state={"product_id": session.get("product_id")},
        )
        if result["intent"] == "CATALOG":
            return self._catalog(session)
        if result["intent"] == "UNKNOWN":
            session.update(stage="human_offer", question=text)
            return Reply(
                "По этому вопросу требуется ответ человека. "
                "Можно оставить контакт и передать исходный вопрос.",
                [self._button(session, "Оставить вопрос человеку", "human")],
            )
        if result["product_id"] is None:
            if result["match_status"] == "not_found":
                return Reply("Такого товара нет в каталоге.", [("Каталог", "ui:catalog")])
            choices = result["candidates"][:20]
            session["choices"] = [choice["product_id"] for choice in choices]
            session.update(pending_parse=result, question=text)
            return Reply(
                "Подтвердите товар из кандидатов." if choices
                else "Выберите, пожалуйста, товар в каталоге.",
                [
                    self._button(session, choice["name"][:100], f"p{index}")
                    for index, choice in enumerate(choices)
                ] if choices else list(CUSTOMER_HOME_BUTTONS),
            )
        product = self.product(result["product_id"])
        if result["intent"] in ("PRICE", "SIZES", "AVAILABILITY"):
            return self._information(session, text, result)
        session.update(product_id=product.product_id, product_name=product.name)
        session["question"] = text
        if result["intent"] == "ORDER" and result["size_status"] == "recognized":
            session.update(size=result["size"], stage="name")
            return Reply(product_text(product) + "\nВведите имя покупателя.")
        reply = self._sizes(session)
        if product.availability != "out_of_stock" and result["size_status"] in ("unavailable", "unrecognized"):
            message = (
                "Запрошенный размер отсутствует в наличии по каталогу."
                if product.stock_known and result["size_status"] == "unavailable"
                else "Запрошенный размер не подтверждён для этого товара."
            )
            reply.text = message + "\n" + reply.text
        return reply

    def _back(self, session: dict) -> Reply:
        stage = session["stage"]
        if stage == "confirm":
            session.pop("contact", None)
            session["stage"] = "contact"
            return Reply("Введите контакт заново.")
        if stage == "contact":
            session.pop("name", None)
            session["stage"] = "name"
            return Reply("Введите имя заново.")
        if stage == "name" and not session.get("human"):
            session.pop("size", None)
            return self._sizes(session)
        if stage in ("done", "cancelled"):
            return self._home_reply("Оформление завершено. Выберите, что хотите сделать дальше.")
        # Rotate operation ID so old choice/confirm buttons cannot affect a new flow.
        self._new(session)
        return self._catalog(session)

    def _admin(
        self, db: sqlite3.Connection, user_id: int,
        text: str, callback: str | None,
    ) -> Reply:
        if self.admin_id is None or user_id != self.admin_id:
            return Reply("Доступ запрещён.")
        parts = callback.split(":") if callback else text.split()
        command = parts[1] if len(parts) > 1 else "menu"
        if command in ("list", "pending"):
            page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            page = max(0, page)
            where = " WHERE notification_state != 'sent'" if command == "pending" else ""
            rows = db.execute(
                "SELECT * FROM applications" + where
                + " ORDER BY id DESC LIMIT 6 OFFSET ?", (page * 5,),
            ).fetchall()
            lines = [
                f"Заявка №{row['id']}\nСтатус: {row['status']}\n"
                f"Товар: {row['product_name'] or 'Обращение к человеку'}\n"
                f"Уведомление: {NOTIFICATION_LABELS[row['notification_state']]}"
                for row in rows[:5]
            ]
            buttons = [(f"Открыть №{r['id']}", f"admin:view:{r['id']}") for r in rows[:5]]
            if page:
                buttons.append(("Предыдущие", f"admin:{command}:{page - 1}"))
            if len(rows) > 5:
                buttons.append(("Следующие", f"admin:{command}:{page + 1}"))
            title = "Неотправленные уведомления" if command == "pending" else "Список заявок"
            return Reply(title + "\n\n" + ("\n\n".join(lines) or "Заявок нет."), buttons)
        if command == "stats":
            total = db.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
            statuses = dict(db.execute(
                "SELECT status, COUNT(*) FROM applications GROUP BY status",
            ).fetchall())
            products = db.execute(
                "SELECT COALESCE(product_id, 'Вопрос человеку') AS product, "
                "COUNT(*) AS n FROM applications GROUP BY product_id "
                "ORDER BY n DESC LIMIT 30",
            ).fetchall()
            product_names = {product.product_id: product.name for product in self.catalog.products}
            return Reply(
                f"Всего заявок: {total}\nПо статусам:\n"
                + "\n".join(f"{status}: {statuses.get(status, 0)}" for status in STATUSES)
                + "\nПо товарам (до 30 позиций):\n"
                + "\n".join(f"{product_names.get(row['product'], row['product'])}: {row['n']}" for row in products)
            )
        if command in ("view", "status", "retry"):
            if len(parts) < 3 or not parts[2].isdigit():
                return Reply("Укажите числовой ID заявки.")
            application_id = int(parts[2])
            row = db.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
            if not row:
                return Reply("Заявка не найдена.")
            if command == "status":
                if len(parts) < 4 or parts[3] not in ("0", "1", "2"):
                    return Reply("Статус: 0 — Новая, 1 — Нужен ответ человека, 2 — Обработана.")
                db.execute(
                    "UPDATE applications SET status=? WHERE id=?",
                    (STATUSES[int(parts[3])], application_id),
                )
                return Reply(f"Заявка №{application_id}: установлен статус «{STATUSES[int(parts[3])]}».")
            if command == "retry":
                if row["notification_state"] == "sent":
                    return Reply("Уведомление уже отправлено.")
                return Reply("Повтор уведомления запрошен.", application_id=application_id)
            detail = (
                f"Заявка №{row['id']}\nСтатус: {row['status']}\n"
                f"Покупатель: {row['customer_name']}\nКонтакт: {row['contact']}\n"
                f"Товар: {row['product_name'] or 'Обращение к человеку'}\n"
                f"Размер: {row['size'] or '—'}\n"
            )
            question = row["original_question"]
            if question and (not row["size"] or not question.startswith("Уточнить наличие и размеры:")):
                detail += f"Вопрос: {question}\n"
            detail += (
                f"Уведомление: {NOTIFICATION_LABELS[row['notification_state']]}\n"
                f"Попыток отправки: {row['notification_attempts']}\n"
                f"Ошибка отправки: {row['notification_error'] or '—'}"
            )
            return Reply(detail, [
                (status, f"admin:status:{application_id}:{index}")
                for index, status in enumerate(STATUSES)
            ] + ([("Повтор уведомления", f"admin:retry:{application_id}")]
                 if row["notification_state"] != "sent" else []))
        if command == "catalog":
            return Reply(
                "\n\n".join(product_text(p) for p in self.catalog.visible_products[:15])
                or "Каталог пуст. Используйте /admin_reload после изменения файла.",
            )
        if command == "reload":
            return Reply("Перезагрузка доступна командой /admin_reload.")
        return Reply("Меню администратора", [
            ("Заявки", "admin:list"), ("Неотправленные уведомления", "admin:pending"),
            ("Статистика", "admin:stats"), ("Каталог (до 15 товаров)", "admin:catalog"),
        ])

    def reload_catalog(self, user_id: int, path: str) -> Reply:
        if self.admin_id is None or user_id != self.admin_id:
            return Reply("Доступ запрещён.")
        catalog = load_catalog(path)  # Replace only after successful validation.
        self.catalog = catalog
        return Reply(f"Каталог загружен: {len(catalog.visible_products)} активных товаров.")
