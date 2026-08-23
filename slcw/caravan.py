"""Caravan trade: what a load is worth, and where it is legal to take it.

Every city runs one workshop. It turns out a single refined good and consumes
two others, and its warehouse prices both sides off how much it is holding: a
warehouse with nothing left pays twice the base price, one that is full pays
half. `dispatchCaravan` buys the origin's output with gold — the cargo never
touches the bag — carries it for twenty seconds per unit of map distance, and
sells it at the destination's price on arrival.

The routing rule is the server's, learned by being refused: *"Caravans from
cities must go to Hub."* Standing in an ordinary city the only legal
destination is Virtan, the one trade hub, which pays a flat 80% of base for
anything. Standing in Virtan, every city that consumes the good is legal. So
the shape of the trade is a spoke: ride out from the hub loaded with what a
city is starving for, ride back with whatever that city makes.

The numbers below are the client's own, read out of the bundle rather than
fitted. The one thing not modelled here is robbery — cities carry a
`caravanRobberyDefenseBonus`, so it exists, but no chance and no loss appear
anywhere the client can see.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import world

HUB = "city_2"

# Ten units without a merchant pass, and twenty energy without the logistics
# talent: floor(10 + 2*talents.capacity + mount cargo) and
# max(2, 20 - 0.18*talents.logistics).
BASE_CAPACITY = 10
DISPATCH_ENERGY = 20

# Caravans move at half a traveller's pace: a wallet walking city_14 -> city_2
# took 10 seconds per unit of distance, the caravan over the same road took 20.
SECONDS_PER_DISTANCE = 20

BASE_PRICE = {
    "runic_alloy": 4800,
    "crystal_core": 3700,
    "frozen_vein": 3500,
    "aqua_vitae": 3000,
    "living_timber": 1900,
    "aether_fragment": 4000,
    "volt_essence": 3800,
    "runestone": 5500,
    "chronicle_page": 4200,
    "battle_ember": 4500,
    "imperial_seal": 3500,
}

# Which cities consume each good, straight from the client. The hub is left out
# because it is not a workshop — it buys everything at a flat rate instead.
CONSUMERS = {
    "crystal_core": ("city_1", "city_11"),
    "frozen_vein": ("city_1", "city_4", "city_18"),
    "volt_essence": ("city_1", "city_3"),
    "runic_alloy": ("city_4", "city_15", "city_5"),
    "living_timber": ("city_11", "city_14"),
    "aqua_vitae": ("city_5", "city_14", "city_13"),
    "aether_fragment": ("city_15", "city_7"),
    "runestone": ("city_18", "city_7"),
    "chronicle_page": ("city_3", "city_17"),
    "battle_ember": ("city_17",),
}

DEFAULT_CAPACITY = 500

# The thinnest margin worth carrying. Prices are read from the `cities`
# documents and can be minutes old by the time the wallet is standing in the
# warehouse, so a leg that only just breaks even on paper is a leg that may
# already have stopped paying. Every measured run cleared 10% or better.
MIN_MARGIN_RATIO = 0.05


def unit_price(template_id: str, held: float, warehouse_capacity: int = DEFAULT_CAPACITY) -> int:
    """What one unit costs or fetches at a warehouse holding `held` of it.

    Twice base when the shelf is empty, half base when it is full, straight
    line between. This is the client's function, not an approximation of it.
    """
    base = BASE_PRICE.get(template_id)
    if not base:
        return 0
    half = max(warehouse_capacity, 1) / 2
    if held <= half:
        multiplier = 2 - held / half
    else:
        multiplier = 1 - 0.5 * min((held - half) / half, 1)
    return math.ceil(base * max(multiplier, 0.5))


def hub_bid(template_id: str) -> int:
    """What Virtan pays for a load, flat.

    Measured 2026-08-23: ten chronicle_page sold there returned exactly 33,600,
    which is ten times floor(0.8 * 4200). The hub's own stock did not enter
    into it, and neither did its 10% tax rate — the wallet kept the whole
    5,710 gold spread.
    """
    return math.floor(0.8 * BASE_PRICE.get(template_id, 0))


def hub_ask(template_id: str) -> int:
    """What Virtan charges for a load, flat.

    Also measured, and the more expensive lesson: reading the client's price
    curve suggested the hub sold its 49,935 battle_ember at the glut price of
    2,256, and the dispatch actually took 5,400 a unit — exactly 1.2 x base.
    The hub is a market maker with a fixed 20% spread either side of base,
    not a warehouse. Estimating its ask from stock overstated the margin on
    every route out of it by a factor of five.
    """
    return math.ceil(1.2 * BASE_PRICE.get(template_id, 0))


# Kept for callers written against the older name.
hub_flat_price = hub_bid


@dataclass(frozen=True)
class Leg:
    """One dispatch: what to buy, how much, and where to take it."""

    template_id: str
    quantity: int
    destination_id: str
    origin_id: str
    unit_cost: int
    unit_revenue: int
    tax_rate: int
    profit: int
    travel_seconds: int

    @property
    def cost(self) -> int:
        return self.unit_cost * self.quantity

    @property
    def gold_per_energy(self) -> float:
        return self.profit / DISPATCH_ENERGY

    def describe(self) -> str:
        return (f"{self.quantity} {self.template_id} "
                f"{world.name_of(self.origin_id)} → {world.name_of(self.destination_id)} "
                f"for {self.profit:,}g on {self.cost:,}g")


def travel_seconds(origin_id: str, destination_id: str) -> int:
    gap = world.distance(origin_id, destination_id)
    if gap == float("inf"):
        return 0
    return round(SECONDS_PER_DISTANCE * gap)


def _stock_for_sale(city: dict) -> dict:
    """What this city will sell a caravan, by template id.

    A workshop sells only what it makes. The hub sells everything it holds, and
    keeps it under `outputs` rather than `output`.
    """
    warehouse = city.get("warehouse") or {}
    if warehouse.get("isTradeHub"):
        return {row.get("templateId"): row.get("quantity", 0)
                for row in (warehouse.get("outputs") or [])
                if row.get("templateId")}
    output = warehouse.get("output") or {}
    template = output.get("templateId")
    return {template: output.get("quantity", 0)} if template else {}


def _demand_for(city: dict, template_id: str) -> int:
    warehouse = city.get("warehouse") or {}
    for row in (warehouse.get("input") or []):
        if row.get("templateId") == template_id:
            return row.get("quantity", 0)
    return 0


def legal_destinations(origin_id: str, origin: dict, template_id: str) -> list[str]:
    """Where the server will let this cargo go.

    Refusing wrong destinations here rather than discovering them from
    `dispatchCaravan` keeps a rejected call from counting against the wallet's
    error budget.
    """
    warehouse = origin.get("warehouse") or {}
    if not warehouse.get("isTradeHub"):
        return [HUB] if origin_id != HUB else []
    return [city for city in CONSUMERS.get(template_id, ()) if city != origin_id]


def price_leg(origin_id: str, origin: dict, destination_id: str, destination: dict,
              template_id: str, quantity: int) -> Leg | None:
    """Price one load end to end, or None if it cannot be carried at all."""
    if quantity < 1 or template_id not in BASE_PRICE:
        return None

    for_sale = _stock_for_sale(origin)
    if template_id not in for_sale:
        return None
    held = for_sale[template_id]
    if held < quantity:
        return None

    if (origin.get("warehouse") or {}).get("isTradeHub"):
        # Flat ask: the hub's own stock limits how much it will hand over, but
        # never what it charges.
        cost = hub_ask(template_id) * quantity
    else:
        origin_capacity = origin.get("warehouseCapacity") or DEFAULT_CAPACITY
        # A whole load is priced once, off the shelf as it stands at dispatch.
        # The client walks the curve down a unit at a time and the server does
        # not: ten chronicle_page off a shelf of 418 cost 27,890, which is ten
        # times the price of the shelf at 418, not the sum of ten falling
        # prices. The stock only matters between dispatches.
        cost = unit_price(template_id, held, origin_capacity) * quantity

    if destination_id == HUB:
        revenue = hub_bid(template_id) * quantity
    else:
        capacity = destination.get("warehouseCapacity") or DEFAULT_CAPACITY
        already = _demand_for(destination, template_id)
        revenue = unit_price(template_id, already, capacity) * quantity

    # The client subtracts the destination's taxRate from the profit. Selling
    # into the hub demonstrably does not: Virtan's rate is 10% and the measured
    # leg kept every gold of its spread. Whether a workshop destination charges
    # it has not been measured, so it is still subtracted there — an estimate
    # that comes in under the takings is the safe direction to be wrong in.
    tax = 0 if destination_id == HUB else (destination.get("taxRate") or 0)
    gross = revenue - cost
    profit = gross - (math.floor(tax / 100 * gross) if tax > 0 and gross > 0 else 0)

    return Leg(
        template_id=template_id,
        quantity=quantity,
        destination_id=destination_id,
        origin_id=origin_id,
        unit_cost=cost // quantity,
        unit_revenue=revenue // quantity,
        tax_rate=tax,
        profit=profit,
        travel_seconds=travel_seconds(origin_id, destination_id),
    )


def best_leg(origin_id: str, cities: dict, gold: int,
             capacity: int = BASE_CAPACITY) -> Leg | None:
    """The most profitable load this wallet can afford to dispatch from here.

    `cities` maps city id to its live document. A load is shrunk to what the
    wallet can pay for rather than skipped, because a half load of the best
    route still beats a full load of a worse one.
    """
    origin = cities.get(origin_id)
    if not origin or capacity < 1 or gold <= 0:
        return None

    best: Leg | None = None
    for template_id, held in _stock_for_sale(origin).items():
        if template_id not in BASE_PRICE:
            continue
        for destination_id in legal_destinations(origin_id, origin, template_id):
            destination = cities.get(destination_id)
            if destination is None:
                continue
            quantity = min(capacity, int(held))
            leg = None
            while quantity >= 1:
                candidate = price_leg(origin_id, origin, destination_id,
                                      destination, template_id, quantity)
                if candidate is not None and candidate.cost <= gold:
                    leg = candidate
                    break
                quantity -= 1
            if leg is None or leg.profit <= max(1, leg.cost * MIN_MARGIN_RATIO):
                continue
            if best is None or leg.profit > best.profit:
                best = leg
    return best


def ranked_origins(cities: dict, gold: int,
                   capacity: int = BASE_CAPACITY) -> list[tuple[str, Leg]]:
    """Every city worth standing in and the load it would let us send, best first.

    A list rather than a winner because the caller knows two things this does
    not: how far away each city is, and that thirty wallets picking the same
    argmax would all walk to the same warehouse.
    """
    found = []
    for city_id in cities:
        leg = best_leg(city_id, cities, gold, capacity)
        if leg is not None:
            found.append((city_id, leg))
    found.sort(key=lambda pair: -pair[1].profit)
    return found


def best_origin(cities: dict, gold: int, capacity: int = BASE_CAPACITY) -> tuple[str, Leg] | None:
    """The single most profitable city to stand in, ignoring how far it is."""
    ranked = ranked_origins(cities, gold, capacity)
    return ranked[0] if ranked else None
