"""Local batch harness. Expected fields are assertions, not generated labels."""
import csv
import io
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .parser import parse_message

EXTRA_FIELDS = (
    "requested_size", "size_status", "price", "currency",
    "available_sizes", "availability",
)
FIELDS = {
    "intent", "product_id", "size", "match_status", "needs_clarification", "reason",
    *EXTRA_FIELDS,
}


def _expected(value):
    if value is not None and (not isinstance(value, dict) or not value or set(value) - FIELDS):
        raise ValueError("expected должен быть непустым объектом с полями контракта")
    return value


def read_cases(path: Path, expected_path: Path | None = None) -> list[dict]:
    expected = json.loads(expected_path.read_text(encoding="utf-8-sig")) if expected_path else {}
    if not isinstance(expected, dict):
        raise ValueError("Файл ожиданий: объект с номерами исходных строк в качестве ключей")
    cases = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        if path.suffix.casefold() == ".jsonl":
            record = json.loads(line)
            if not isinstance(record, dict) or not isinstance(record.get("text"), str):
                raise ValueError(f"Строка {number}: нужен объект с text-строкой")
            text = record["text"]
            target = record.get("expected")
        else:
            text, target = line, None
        if str(number) in expected:
            if target is not None:
                raise ValueError(f"Два набора ожиданий для строки {number}")
            target = expected[str(number)]
        cases.append({"line": number, "text": text, "expected": _expected(target)})
    if set(expected) - {str(c["line"]) for c in cases}:
        raise ValueError("В ожиданиях указана отсутствующая или пустая строка")
    return cases


def run_batch(input_path, catalog, output_path, *, state=None, config=None, expected_path=None, catalog_path=None) -> dict:
    input_path, output_path = Path(input_path), Path(output_path)
    expected_path = Path(expected_path) if expected_path else None
    cases = read_cases(input_path, expected_path)
    results = []
    for case in cases:
        result = parse_message(case["text"], catalog, state, config=config)
        target = case["expected"]
        differences = {key: {"expected": value, "actual": result[key]} for key, value in (target or {}).items() if result[key] != value}
        status = "UNREVIEWED" if target is None else "FAIL" if differences else "PASS"
        results.append({**case, "result": result, "status": status, "differences": differences})
    counts = {status: sum(r["status"] == status for r in results) for status in ("PASS", "FAIL", "UNREVIEWED")}
    reviewed = counts["PASS"] + counts["FAIL"]
    summary = {"total": len(results), **counts,
               "reviewed_accuracy": counts["PASS"] / reviewed if reviewed else None,
               "note": "PASS проверяет только явно заданные expected-поля; UNREVIEWED не является успешной проверкой"}
    files = {f"json/{i:06d}.json": (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
             for i, result in enumerate(results, 1)}
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";")
    writer.writerow(["line", "text", "intent", "product_id", "size", "match_status", "needs_clarification", "reason", "status", "expected", "differences", *EXTRA_FIELDS])
    for row in results:
        values = [row["line"], row["text"], *(row["result"][key] for key in ("intent", "product_id", "size", "match_status", "needs_clarification", "reason")),
                  row["status"], json.dumps(row["expected"], ensure_ascii=False), json.dumps(row["differences"], ensure_ascii=False)]
        values.extend(
            json.dumps(row["result"][key], ensure_ascii=False)
            if isinstance(row["result"][key], list) else row["result"][key]
            for key in EXTRA_FIELDS
        )
        cells = ["" if value is None else str(value) for value in values]
        writer.writerow(["'" + cell if cell.lstrip().startswith(("=", "+", "-", "@")) else cell for cell in cells])
    files["results.csv"] = stream.getvalue().encode("utf-8-sig")
    files["summary.json"] = (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    protected = {Path(p).resolve() for p in (input_path, expected_path, catalog_path) if p is not None}
    targets = [output_path / name for name in (*files, "reports.zip")]
    if any(path.resolve() in protected for path in targets):
        raise ValueError("Выходной отчёт совпадает с входом или каталогом")
    output_path.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        target = output_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    with ZipFile(output_path / "reports.zip", "w", compression=ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return summary
