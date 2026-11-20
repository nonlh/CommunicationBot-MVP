import argparse
import csv
import json
import sys
from pathlib import Path

from . import MatchConfig, load_catalog, parse_message
from .batch import run_batch
from .catalog import DEFAULT_CATALOG_PATH


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Автономный парсер обращений покупателей v0.1")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("single", "Одно сообщение -> JSON"), ("batch", "TXT/JSONL -> JSON, CSV, summary и один ZIP")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG_PATH)
        command.add_argument("--state", default="general", help='general или JSON: {"product_id":"..."}')
        command.add_argument("--fuzzy-threshold", type=float, default=78.0)
        command.add_argument("--ambiguity-gap", type=float, default=8.0)
        if name == "single":
            command.add_argument("--text", required=True)
        else:
            command.add_argument("--input", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--expected", type=Path)
    args = parser.parse_args(argv)
    try:
        catalog = load_catalog(args.catalog)
        config = MatchConfig(args.fuzzy_threshold, args.ambiguity_gap)
        state = json.loads(args.state) if args.state.lstrip().startswith("{") else args.state
        if args.command == "single":
            payload = parse_message(args.text, catalog, state, config=config)
        else:
            payload = run_batch(args.input, catalog, args.output, state=state, config=config,
                                expected_path=args.expected, catalog_path=args.catalog)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if args.command == "batch" and payload["FAIL"] else 0
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
