from pathlib import Path

import pytest

from parser_mvp import load_catalog


@pytest.fixture
def parser_catalog_path():
    return Path(__file__).parent.parent / "fixtures/parser_mvp_catalog.csv"


@pytest.fixture
def parser_catalog(parser_catalog_path):
    return load_catalog(parser_catalog_path)
