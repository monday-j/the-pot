from decimal import Decimal as D

import pytest

from app.payouts import Rule, RuleError, compute_payouts, parse_splits, pool_breakdown, pool_cents, VenueCut, venue_breakdown


def rule(**kw):
    base = dict(bracket_fee=500, payout_places=3, fixed_pool=0, bracket_fee_pool_pct=D(100),
                entry_fee_share_pct=D(0), splits=(D(50), D(30), D(20)))
    return Rule(**{**base, **kw})


def test_pool_combines_fixed_bracket_and_entry_share():
    r = rule(fixed_pool=2000, bracket_fee_pool_pct=D(80), entry_fee_share_pct=D(25))
    # 2000 + 80% of (500*10) + 25% of (1000*16)
    assert pool_cents(r, entrants=10, attendees=16, entry_fee=1000) == 2000 + 4000 + 4000


def test_splits_sum_exactly_to_pool():
    pool = 10001
    paid, undistributed = compute_payouts(pool, (D(50), D(30), D(20)), {"a": 1, "b": 2, "c": 3})
    assert sum(paid.values()) == pool and undistributed == 0
    assert paid["a"] >= paid["b"] >= paid["c"]


def test_third_place_tie_shares_third_and_fourth():
    paid, _ = compute_payouts(1000, (D(50), D(30), D(20)), {"a": 1, "b": 2, "c": 3, "d": 3})
    assert paid == {"a": 500, "b": 300, "c": 100, "d": 100}


def test_tie_odd_cent_is_distributed_deterministically():
    paid, _ = compute_payouts(1001, (D(100),), {"a": 1, "b": 1})
    assert sorted(paid.values()) == [500, 501] and paid["a"] == 501


def test_fewer_players_than_places_leaves_undistributed():
    paid, undistributed = compute_payouts(1000, (D(50), D(30), D(20)), {"a": 1, "b": 2})
    assert paid == {"a": 500, "b": 300} and undistributed == 200


def test_players_outside_paid_places_get_nothing():
    paid, _ = compute_payouts(1000, (D(100),), {"a": 1, "b": 2, "c": 3})
    assert paid == {"a": 1000}


def test_validation():
    rule().validate()
    with pytest.raises(RuleError):
        rule(splits=(D(50), D(30))).validate()
    with pytest.raises(RuleError):
        rule(splits=(D(50), D(30), D(30))).validate()
    with pytest.raises(RuleError):
        rule(bracket_fee_pool_pct=D(101)).validate()
    assert parse_splits("50, 30;20") == (D(50), D(30), D(20))
    with pytest.raises(RuleError):
        parse_splits("fifty")


def test_pool_breakdown_components_sum_to_pool():
    r = rule(fixed_pool=2000, bracket_fee_pool_pct=D(80), entry_fee_share_pct=D(25))
    b = pool_breakdown(r, entrants=10, attendees=16, entry_fee=1000)
    assert (b["fixed"], b["bracket_share"], b["entry_share"]) == (2000, 4000, 4000)
    assert b["total"] == pool_cents(r, 10, 16, 1000) == 10000


def test_venue_breakdown_components():
    assert venue_breakdown(VenueCut(flat=2000), 16000, 5000) == {
        "flat": 2000, "entry_share": 0, "bracket_share": 0, "total": 2000}
    assert venue_breakdown(VenueCut(entry_pct=D(10)), 16000, 5000)["total"] == 1600
    assert venue_breakdown(VenueCut(bracket_pct=D(10)), 16000, 5000)["total"] == 500
    combined = venue_breakdown(VenueCut(2000, D(10), D(10)), 16000, 5000)
    assert (combined["flat"], combined["entry_share"], combined["bracket_share"], combined["total"]) == (2000, 1600, 500, 4100)


def test_venue_cut_rounds_half_up_and_validates():
    assert venue_breakdown(VenueCut(entry_pct=D("33.333")), 100, 0)["entry_share"] == 33
    assert venue_breakdown(VenueCut(entry_pct=D("12.5")), 100, 0)["entry_share"] == 13
    assert VenueCut().is_zero and not VenueCut(flat=1).is_zero
    for bad in (VenueCut(flat=-1), VenueCut(entry_pct=D(101)), VenueCut(bracket_pct=D(-1))):
        with pytest.raises(RuleError):
            bad.validate()
