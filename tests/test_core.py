import unittest
from app.core import *

def good(**kw):
    d = dict(product="working_capital", amount=1_000_000, term_months=12, company_age_years=5,
             annual_revenue=24_000_000, revenue_trend_pct=15, bki_grade="A", overdue_count=0, okved_risk="low",
             has_collateral=True)
    d.update(kw); return Application(**d)

def test_auto_approve():
    r = decide(good()); assert r.outcome == "AUTO_APPROVED" and r.grade == "A" and r.offer

def test_counter_offer_when_amount_too_big():
    r = decide(good(amount=9_000_000)); assert r.outcome == "COUNTER_OFFER" and r.offer["amount"] < 9_000_000

def test_needs_info():
    assert decide(good(annual_revenue=None)).outcome == "NEEDS_INFO"

def test_knockout():
    assert decide(good(director_disqualified=True)).outcome == "REJECTED"

def test_manual_review_for_grade_b_or_c():
    r = decide(good(bki_grade="B", revenue_trend_pct=3, company_age_years=2, has_collateral=False, okved_risk="mid"))
    assert r.outcome == "MANUAL_REVIEW"

def test_transitions():
    assert transition(S.DRAFT, S.SUBMITTED) == S.SUBMITTED
    for a, b in [(S.REJECTED, S.APPROVED), (S.DRAFT, S.DISBURSED)]:
        try: transition(a, b); assert False, "должно быть запрещено"
        except InvalidTransition: pass

if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"): f(); print("OK", n)
