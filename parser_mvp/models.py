from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    aliases: tuple[str, ...] = ()
    sizes: tuple[str, ...] = ()
    color: str = ""
    price: Decimal | None = None
    currency: str | None = None
    active: bool = True
    stock_known: bool = False

    @property
    def availability(self) -> str:
        if not self.stock_known:
            return "unknown"
        return "in_stock" if self.sizes else "out_of_stock"

    def __post_init__(self) -> None:
        if self.price is not None and (
            not isinstance(self.price, Decimal)
            or not self.price.is_finite() or self.price < 0
        ):
            raise ValueError("Цена должна быть конечным неотрицательным Decimal")


@dataclass(frozen=True)
class Catalog:
    products: tuple[Product, ...]

    @property
    def visible_products(self) -> tuple[Product, ...]:
        return tuple(product for product in self.products if product.active)

    def __post_init__(self):
        ids = [p.product_id for p in self.products]
        if any(not p.product_id.strip() or not p.name.strip() for p in self.products):
            raise ValueError("У товара обязательны product_id и name")
        if len(ids) != len(set(ids)):
            raise ValueError("В каталоге повторяется product_id")


@dataclass(frozen=True)
class MatchConfig:
    fuzzy_threshold: float = 78.0
    ambiguity_gap: float = 8.0

    def __post_init__(self):
        if not 0 < self.fuzzy_threshold <= 100 or not 0 <= self.ambiguity_gap <= 100:
            raise ValueError("Пороги поиска должны быть в диапазоне 0..100")
