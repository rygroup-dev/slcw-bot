"""Black market order book, valuation, and crossed-spread detection.

Depth accounting subtracts `filled` from `quantity`. The previous monitor summed raw
quantity, so a 565-unit order with 35 already filled counted as 565 rather than 530
and overstated available liquidity.
"""
from __future__ import annotations

import collections
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA

SNAPSHOT_PATH = DATA / "market_snapshot.json"


@dataclass
class BookLevel:
    price: float
    quantity: int


@dataclass
class Book:
    template_id: str
    bids: list = field(default_factory=list)
    asks: list = field(default_factory=list)

    @property
    def best_bid(self) -> float | None:
        return max((lvl.price for lvl in self.bids), default=None)

    @property
    def best_ask(self) -> float | None:
        return min((lvl.price for lvl in self.asks), default=None)

    @property
    def spread(self) -> float | None:
        """Conventional spread: ask minus bid. Negative means the book is crossed."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def is_crossed(self) -> bool:
        return self.spread is not None and self.spread < 0

    def bid_depth(self, price_floor: float = 0.0) -> int:
        return sum(lvl.quantity for lvl in self.bids if lvl.price >= price_floor)


@dataclass
class MarketSnapshot:
    books: dict = field(default_factory=dict)
    taken_at: int = 0

    @property
    def age_seconds(self) -> float:
        return time.time() - self.taken_at if self.taken_at else float("inf")

    def is_fresh(self, ttl_seconds: int) -> bool:
        return self.age_seconds <= ttl_seconds

    def best_bid(self, template_id: str) -> float | None:
        book = self.books.get(template_id)
        return book.best_bid if book else None

    def value_of(self, items: dict) -> float:
        """Liquidation value of an item bundle at current best bids."""
        total = 0.0
        for template_id, quantity in items.items():
            bid = self.best_bid(template_id)
            if bid:
                total += bid * quantity
        return total

    def crossed(self) -> list:
        return [book for book in self.books.values() if book.is_crossed]


def build_snapshot(orders: list[dict]) -> MarketSnapshot:
    grouped: dict = collections.defaultdict(lambda: Book(template_id=""))
    for order in orders:
        if order.get("status") != "open":
            continue
        template_id = order.get("templateId")
        price = order.get("price")
        side = order.get("type")
        if not template_id or side not in ("buy", "sell") or not isinstance(price, (int, float)):
            continue
        remaining = int(order.get("quantity", 0) or 0) - int(order.get("filled", 0) or 0)
        if remaining <= 0:
            continue
        book = grouped[template_id]
        book.template_id = template_id
        level = BookLevel(price=float(price), quantity=remaining)
        (book.bids if side == "buy" else book.asks).append(level)
    return MarketSnapshot(books=dict(grouped), taken_at=int(time.time()))


def save_snapshot(snapshot: MarketSnapshot) -> None:
    payload = {
        "taken_at": snapshot.taken_at,
        "books": {
            template_id: {
                "best_bid": book.best_bid,
                "best_ask": book.best_ask,
                "spread": book.spread,
                "crossed": book.is_crossed,
                "bid_depth": book.bid_depth(),
                "ask_depth": sum(lvl.quantity for lvl in book.asks),
            }
            for template_id, book in snapshot.books.items()
        },
    }
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(SNAPSHOT_PATH)


def load_snapshot() -> MarketSnapshot:
    if not SNAPSHOT_PATH.exists():
        return MarketSnapshot()
    try:
        payload = json.loads(SNAPSHOT_PATH.read_text())
    except json.JSONDecodeError:
        return MarketSnapshot()
    books = {}
    for template_id, entry in (payload.get("books") or {}).items():
        book = Book(template_id=template_id)
        if entry.get("best_bid") is not None:
            book.bids.append(BookLevel(float(entry["best_bid"]), int(entry.get("bid_depth", 0))))
        if entry.get("best_ask") is not None:
            book.asks.append(BookLevel(float(entry["best_ask"]), int(entry.get("ask_depth", 0))))
        books[template_id] = book
    return MarketSnapshot(books=books, taken_at=int(payload.get("taken_at", 0)))


def inventory_holdings(inventory: dict) -> dict:
    holdings: collections.Counter = collections.Counter()
    for slot in inventory.get("slots") or []:
        if isinstance(slot, dict) and slot.get("templateId"):
            holdings[slot["templateId"]] += int(slot.get("quantity", 0) or 0)
    return dict(holdings)
