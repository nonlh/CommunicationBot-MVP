import csv
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from parser_mvp.batch import run_batch

ROOT = Path(__file__).resolve().parents[2]


def test_batch_pass_fail_unreviewed_and_one_zip(tmp_path, parser_catalog):
    source = tmp_path / "input.jsonl"
    source.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in [
        {"text": "Какая цена футболки синей?", "expected": {"intent": "PRICE", "product_id": "TEST-BLUE"}},
        {"text": "Из какого материала футболка?", "expected": {"intent": "PRICE"}},
        {"text": "=unreviewed phrase"},
    ]), encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    (output / ".env").write_text("SECRET=must-not-be-archived", encoding="utf-8")
    summary = run_batch(source, parser_catalog, output)
    assert (summary["PASS"], summary["FAIL"], summary["UNREVIEWED"]) == (1, 1, 1)
    assert summary["reviewed_accuracy"] == 0.5
    assert len(list(output.glob("*.zip"))) == 1
    with zipfile.ZipFile(output / "reports.zip") as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {"json/000001.json", "json/000002.json", "json/000003.json", "results.csv", "summary.json"}
        for name in archive.namelist():
            assert archive.read(name) == (output / name).read_bytes()
    record = json.loads((output / "json/000003.json").read_text(encoding="utf-8"))
    assert record["text"] == "=unreviewed phrase" and record["status"] == "UNREVIEWED"
    with (output / "results.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert list(csv.DictReader(stream, delimiter=";"))[2]["text"].startswith("'=")


def test_txt_and_separate_expectations_do_not_fake_pass(tmp_path, parser_catalog):
    source = tmp_path / "input.txt"
    source.write_text("Покажи каталог\n\nИз чего футболка?\n", encoding="utf-8")
    expected = tmp_path / "expected.json"
    expected.write_text('{"1":{"intent":"CATALOG"}}', encoding="utf-8")
    summary = run_batch(source, parser_catalog, tmp_path / "out", expected_path=expected)
    assert summary["PASS"] == 1 and summary["UNREVIEWED"] == 1
    summary = run_batch(source, parser_catalog, tmp_path / "raw")
    assert summary["PASS"] == 0 and summary["reviewed_accuracy"] is None


@pytest.mark.parametrize("content", ['{"text":5}', '{"text":"hi","expected":{}}', 'not json'])
def test_invalid_batch_is_rejected_before_output(tmp_path, parser_catalog, content):
    source = tmp_path / "input.jsonl"
    source.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        run_batch(source, parser_catalog, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_input_file_cannot_be_overwritten(tmp_path, parser_catalog):
    source = tmp_path / "results.csv"
    source.write_text("Покажи каталог", encoding="utf-8")
    with pytest.raises(ValueError):
        run_batch(source, parser_catalog, tmp_path)
    assert source.read_text(encoding="utf-8") == "Покажи каталог"


def test_real_cli_and_independent_import(parser_catalog_path, tmp_path):
    single = subprocess.run([sys.executable, "-m", "parser_mvp", "single", "--text", "Сколько стоит футболка синяя?", "--catalog", str(parser_catalog_path)],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert single.returncode == 0
    assert json.loads(single.stdout)["product_id"] == "TEST-BLUE"
    source = tmp_path / "input.jsonl"
    source.write_text('{"text":"Покажи каталог","expected":{"intent":"PRICE"}}', encoding="utf-8")
    batch = subprocess.run([sys.executable, "-m", "parser_mvp", "batch", "--input", str(source), "--catalog", str(parser_catalog_path), "--output", str(tmp_path / "out")],
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert batch.returncode == 1 and json.loads(batch.stdout)["FAIL"] == 1
    imported = subprocess.run([sys.executable, "-c", "from parser_mvp import parse_message; import sys; assert 'parser_mvp.__main__' not in sys.modules; assert 'sales_priority' not in sys.modules"], cwd=ROOT)
    assert imported.returncode == 0
