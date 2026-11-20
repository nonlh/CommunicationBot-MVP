"""Autonomous message parser v0.1; independent of sales_priority and CLI."""
from .catalog import load_catalog
from .models import Catalog, MatchConfig, Product
from .parser import parse_message

__all__ = ["parse_message", "load_catalog", "Catalog", "Product", "MatchConfig"]
