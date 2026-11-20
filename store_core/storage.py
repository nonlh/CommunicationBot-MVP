"""SQLite transactions persist FSM, event receipts, applications and outbox."""
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("Новая", "Нужен ответ человека", "Обработана")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    user_id INTEGER PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    operation_id TEXT NOT NULL UNIQUE,
                    confirmation_event TEXT NOT NULL UNIQUE,
                    customer_name TEXT NOT NULL,
                    contact TEXT NOT NULL,
                    product_id TEXT,
                    product_name TEXT,
                    size TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN ('Новая', 'Нужен ответ человека', 'Обработана')
                    ),
                    original_question TEXT,
                    notification_state TEXT NOT NULL DEFAULT 'pending'
                        CHECK (notification_state IN ('pending', 'failed', 'sent')),
                    notification_attempts INTEGER NOT NULL DEFAULT 0,
                    notification_error TEXT,
                    notified_at TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    response TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def session(db: sqlite3.Connection, user_id: int) -> dict:
        row = db.execute("SELECT data FROM sessions WHERE user_id=?", (user_id,)).fetchone()
        return json.loads(row["data"]) if row else {}

    @staticmethod
    def save_session(db: sqlite3.Connection, user_id: int, session: dict) -> None:
        db.execute(
                "INSERT INTO sessions VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
            (user_id, json.dumps(session, ensure_ascii=False)),
        )

    @staticmethod
    def create_application(
        db: sqlite3.Connection, user_id: int, event_id: str, session: dict,
    ) -> int:
        """Called only by the confirmation transition, in the event transaction."""
        db.execute("""
            INSERT INTO applications (
                created_at, user_id, operation_id, confirmation_event,
                customer_name, contact, product_id, product_name, size,
                status, original_question
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(operation_id) DO NOTHING
        """, (
            utc_now(), user_id, session["operation"], event_id,
            session["name"], session["contact"], session.get("product_id"),
            session.get("product_name"), session.get("size"),
            STATUSES[1] if session.get("human") else STATUSES[0],
            session.get("question"),
        ))
        row = db.execute(
            "SELECT id FROM applications WHERE operation_id=? AND user_id=?",
            (session["operation"], user_id),
        ).fetchone()
        return row["id"]

    def get(self, application_id: int) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM applications WHERE id=?", (application_id,),
            ).fetchone()
            return dict(row) if row else None

    def applications(self, *, pending: bool = False) -> list[dict]:
        query = "SELECT * FROM applications"
        if pending:
            query += " WHERE notification_state != 'sent'"
        with self.connection() as db:
            return [dict(row) for row in db.execute(query + " ORDER BY id")]

    def notification_result(self, application_id: int, error: str | None) -> None:
        with self.connection() as db:
            db.execute("""
                UPDATE applications SET notification_state=?, notification_error=?,
                    notification_attempts=notification_attempts+1, notified_at=?
                WHERE id=?
            """, ("failed" if error else "sent", error, None if error else utc_now(), application_id))
