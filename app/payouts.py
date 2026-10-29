"""Pure payout maths. All money is integer cents; percentages are Decimal."""
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

HUNDRED = Decimal(100)


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    bracket_fee: int = 0
    payout_places: int = 3
    fixed_pool: int = 0
    bracket_fee_pool_pct: Decimal = Decimal(0)
    entry_fee_share_pct: Decimal = Decimal(0)
    splits: tuple = (Decimal(100),)

    def validate(self):
        if self.payout_places < 1:
            raise RuleError("Payout places must be at least 1")
        if len(self.splits) != self.payout_places:
            raise RuleError(f"Need {self.payout_places} split percentages, got {len(self.splits)}")
        if sum(self.splits) != HUNDRED:
            raise RuleError(f"Splits must add up to 100, got {sum(self.splits)}")
        if any(s < 0 for s in self.splits):
            raise RuleError("Splits cannot be negative")
        for name, pct in (("Bracket fee pool %", self.bracket_fee_pool_pct),
                          ("Venue fee share %", self.entry_fee_share_pct)):
            if not Decimal(0) <= pct <= HUNDRED:
                raise RuleError(f"{name} must be between 0 and 100")
        if min(self.bracket_fee, self.fixed_pool) < 0:
            raise RuleError("Amounts cannot be negative")


def parse_splits(text: str) -> tuple:
    try:
        return tuple(Decimal(p.strip()) for p in text.replace(";", ",").split(",") if p.strip())
    except Exception:
        raise RuleError("Splits must be numbers separated by commas, e.g. 50,30,20")


def splits_to_json(splits) -> str:
    return json.dumps([str(s) for s in splits])


def splits_from_json(text: str) -> tuple:
    return tuple(Decimal(s) for s in json.loads(text))


def _cents(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def pool_breakdown(rule: Rule, entrants: int, attendees: int, entry_fee: int) -> dict:
    """The pool's components in cents: fixed pool, share of bracket fees, share of venue fees, and their total."""
    bracket_part = Decimal(rule.bracket_fee * entrants) * rule.bracket_fee_pool_pct / HUNDRED
    entry_part = Decimal(entry_fee * attendees) * rule.entry_fee_share_pct / HUNDRED
    parts = {"fixed": rule.fixed_pool, "bracket_share": _cents(bracket_part), "entry_share": _cents(entry_part)}
    return {**parts, "total": sum(parts.values())}


def pool_cents(rule: Rule, entrants: int, attendees: int, entry_fee: int) -> int:
    """Fixed pool + share of bracket fees collected + share of venue fees collected."""
    return pool_breakdown(rule, entrants, attendees, entry_fee)["total"]


def _place_amounts(pool: int, splits) -> list:
    """Split pool across places, exact to the cent (largest remainder, earlier place wins ties)."""
    raw = [Decimal(pool) * s / HUNDRED for s in splits]
    floors = [int(r) for r in raw]
    leftover = pool - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - floors[i]), i))
    for i in order[:leftover]:
        floors[i] += 1
    return floors


def compute_payouts(pool: int, splits, placements: dict):
    """placements: {player_id: placement}. Returns ({player_id: cents}, undistributed_cents).

    Tied players share the combined amount of the places they occupy (two 3rds
    split 3rd+4th). Places nobody filled are left undistributed.
    """
    amounts = _place_amounts(pool, splits)
    groups = {}
    for player, place in placements.items():
        groups.setdefault(place, []).append(player)
    result = {}
    for place in sorted(groups):
        players = sorted(groups[place])
        lo, hi = place - 1, place - 1 + len(players)
        share = sum(amounts[lo:min(hi, len(amounts))])
        if lo >= len(amounts) or share == 0:
            continue
        base, extra = divmod(share, len(players))
        for i, player in enumerate(players):
            result[player] = base + (1 if i < extra else 0)
    return result, pool - sum(result.values())


@dataclass(frozen=True)
class VenueCut:
    """What the venue takes per tournament: a flat amount plus shares of the fees collected."""
    flat: int = 0
    entry_pct: Decimal = Decimal(0)
    bracket_pct: Decimal = Decimal(0)

    def validate(self):
        if self.flat < 0:
            raise RuleError("Venue flat amount cannot be negative")
        for name, pct in (("Venue's % of venue fees", self.entry_pct), ("Venue's % of bracket fees", self.bracket_pct)):
            if not Decimal(0) <= pct <= HUNDRED:
                raise RuleError(f"{name} must be between 0 and 100")

    @property
    def is_zero(self) -> bool:
        return self.flat == 0 and self.entry_pct == 0 and self.bracket_pct == 0


def venue_breakdown(cut: VenueCut, entry_fees_collected: int, bracket_fees_collected: int) -> dict:
    """The venue's components in cents: flat, share of venue fees, share of bracket fees, and their total."""
    parts = {"flat": cut.flat,
             "entry_share": _cents(Decimal(entry_fees_collected) * cut.entry_pct / HUNDRED),
             "bracket_share": _cents(Decimal(bracket_fees_collected) * cut.bracket_pct / HUNDRED)}
    return {**parts, "total": sum(parts.values())}
