"""Selling refined goods to the Black Market, which is where the gold is.

The bot could gather and it could refine, and then it stopped: there was no way
to turn any of it into money. Pulling every open order on 2026-08-22 showed why
that mattered and where the way out was.

The player market is empty — 3,495 orders across three distinct items, none of
them anything the fleet holds, and creating an order there needs premium
currency no wallet has. The Black Market is not: 6,000 open orders, and its buy
side is refined goods and nothing else. Raw ore, logs, hides and monster drops
have no bid anywhere. Refined ones have thousands of units of standing demand —
copper_ingot at 899 with 6,324 wanted, mithril_ingot at 3,300, echo_ferocity at
15,001.

`executeBlackMarketOrder` fills against that book instantly, is paid in gold,
costs no premium and needs no travel. Measured: five copper_ingot returned
totalGold 4,495 with tax 899, and the balance rose by 3,596.
"""
from __future__ import annotations

from dataclasses import dataclass

from .refining import WORKSHOPS

# Every workshop output, in every tier. This is the whole set of things the
# Black Market bids on, which is not a coincidence: refining is what the game
# wants players to do, and the order book is where it pays them for it.
REFINED_ITEMS = frozenset(
    item for workshop in WORKSHOPS.values() for item in workshop.items)

# The Black Market keeps a fifth of the sale. Measured, not assumed: tax 899 on
# a quote of 4,495.
TAX_RATE = 0.20

# Refined goods are also what the crafting bench turns into equipment, and one
# piece of t2 gear sold back for 8,948 — ten times what an ingot fetches. That
# was the reasoning for holding ten of every stack back.
#
# It cost more than it saved. This fleet has never crafted anything — not once
# in its whole ledger — and the bench is out of reach anyway: the recipes want
# ingots the bot does not make, every profession sits at level 0, and 88 of 154
# recipes need echo_* items nobody has. Meanwhile a refining run produces two
# units at a time, so `2 - 10` is negative and no sale is ever offered. Three
# wallets were sitting on 1, 2 and 4 rough_leather with a standing bid of 1,000
# a unit and 1,626 units of demand, and the fleet's entire history contains one
# filled order.
#
# So the reserve is now the caller's decision and defaults to nothing. Raise
# SLCW_CRAFTING_RESERVE the day crafting is real.
CRAFTING_RESERVE = 0

# Below this a sale is not worth the cycle it costs; the wallet has better
# things to do with the turn.
MIN_SALE_GOLD = 500


def is_refined(item_id: str) -> bool:
    return item_id in REFINED_ITEMS


def net_proceeds(gross: float) -> int:
    """What actually lands in the balance after the market's cut."""
    return int(gross - int(gross * TAX_RATE))


@dataclass
class BlackMarketSale:
    item: str
    quantity: int
    gross: int
    net: int


def next_sale(holdings: dict, market, reserve: int = CRAFTING_RESERVE):
    """The most valuable refined stack worth selling, or None.

    Valued at the best standing bid rather than the depth-weighted average: the
    order is filled against the book by the server, and a quote taken from the
    top of it is the honest upper bound on a decision this only uses for
    ranking.
    """
    if market is None or not holdings:
        return None

    best = None
    for item, quantity in (holdings or {}).items():
        if not is_refined(item):
            continue
        sellable = int(quantity or 0) - max(0, int(reserve))
        if sellable <= 0:
            continue
        bid = market.best_bid(item) or 0.0
        if bid <= 0:
            continue
        gross = int(bid * sellable)
        net = net_proceeds(gross)
        if net < MIN_SALE_GOLD:
            continue
        if best is None or net > best.net:
            best = BlackMarketSale(item, sellable, gross, net)
    return best
