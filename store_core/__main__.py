import argparse
import json
import logging
import sys
from dataclasses import replace

from .config import Settings


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="CommunicationBot store core")
    parser.add_argument("command", choices=("bot", "simulate"))
    parser.add_argument("--catalog", default=None)
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # aiogram INFO logs contain user IDs; suppress routine framework logs.
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
        if args.catalog is not None:
            settings = replace(settings, catalog_path=args.catalog)
        if args.command == "bot" and args.db is not None:
            settings = replace(settings, sqlite_path=args.db)
        if args.command == "simulate":
            from .simulator import run

            result = run(args.catalog or settings.catalog_path, args.db or "data/simulation.sqlite3")
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        else:
            from .telegram import main as bot_main

            bot_main(settings)
    except (ValueError, OSError) as exc:
        # Do not render exceptions from token validation/network clients.
        if args.command == "bot":
            logging.error("Запуск не выполнен. Проверьте token, admin ID, пути и формат каталога. Без токена: python -m store_core simulate")
        else:
            logging.error("Симуляция не выполнена: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
