"""Environment configuration; never logs or persists the bot token."""
import os
from dataclasses import dataclass, field

from parser_mvp.catalog import DEFAULT_CATALOG_PATH


def _flag(name: str) -> bool:
    value = os.getenv(name, "0").casefold()
    if value not in ("0", "1", "true", "false"):
        raise ValueError(f"{name}: ожидается 0/1 или true/false")
    return value in ("1", "true")


@dataclass(frozen=True)
class Settings:
    token: str = field(default="", repr=False)
    admin_id: int | None = None
    catalog_path: str = str(DEFAULT_CATALOG_PATH)
    sqlite_path: str = "data/store.sqlite3"
    notification_backend: str = "local"
    test_mode: bool = False
    fail_first: bool = False

    def __post_init__(self) -> None:
        if self.fail_first and not self.test_mode:
            raise ValueError("Имитация ошибки доступна только в STORE_TEST_MODE=1")
        if self.notification_backend not in ("local", "telegram"):
            raise ValueError("NOTIFICATION_BACKEND: local или telegram")
        if self.notification_backend == "telegram" and self.admin_id is None:
            raise ValueError("Для Telegram-уведомлений нужен TELEGRAM_ADMIN_ID")
        if self.admin_id is not None and self.admin_id <= 0:
            raise ValueError("TELEGRAM_ADMIN_ID должен быть положительным user ID")

    @classmethod
    def from_env(cls) -> "Settings":
        admin = os.getenv("TELEGRAM_ADMIN_ID", "").strip()
        return cls(
            token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            admin_id=int(admin) if admin else None,
            catalog_path=os.getenv("CATALOG_PATH", str(DEFAULT_CATALOG_PATH)),
            sqlite_path=os.getenv("SQLITE_PATH", "data/store.sqlite3"),
            notification_backend=os.getenv("NOTIFICATION_BACKEND", "local"),
            test_mode=_flag("STORE_TEST_MODE"),
            fail_first=_flag("NOTIFICATION_FAIL_FIRST"),
        )
