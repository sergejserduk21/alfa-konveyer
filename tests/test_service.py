"""Сквозные сценарии на уровне сервиса (без HTTP)."""
from app import service as sv
from app.core import InvalidTransition
from app.mocks import SCENARIOS


def setup():
    c = sv.connect(); sv.seed(c)
    cl = sv.user_by_token(c, sv.login(c, "client", "client123")["token"])
    an = sv.user_by_token(c, sv.login(c, "analyst", "analyst123")["token"])
    return c, cl, an


def run(c, cl, n):
    s = SCENARIOS[n - 1]
    a = sv.create_application(c, cl, s["inn"], s["product"], s["amount"], s["term_months"])
    assert a["status"] == "DRAFT"
    return sv.submit(c, cl, a["id"])


def test_1_auto_approve_and_full_path():
    c, cl, an = setup(); a = run(c, cl, 1)
    assert a["status"] == "APPROVED" and a["decision"]["auto"] and "score" not in a["decision"]
    a = sv.respond(c, cl, a["id"], True); assert a["status"] == "OFFER_ACCEPTED"
    a = sv.sign(c, cl, a["id"]); assert a["status"] == "DISBURSED"
    assert [e["to"] for e in a["events"]][-1] == "DISBURSED" and len(a["events"]) >= 8


def test_2_counter_offer():
    c, cl, an = setup(); a = run(c, cl, 2)
    assert a["status"] == "COUNTER_OFFER"
    o = a["decision"]["offer"]; assert o["amount"] < 4_000_000 and o["term_months"] > 24
    assert sv.respond(c, cl, a["id"], False)["status"] == "CANCELLED"


def test_3_needs_info_then_auto():
    c, cl, an = setup(); a = run(c, cl, 3)
    assert a["status"] == "NEEDS_INFO" and {r["field"] for r in a["decision"]["requested"]} >= {"annual_revenue"}
    a = sv.provide_info(c, cl, a["id"], {"annual_revenue": 18_000_000, "revenue_trend_pct": 12})
    assert a["status"] == "APPROVED"


def test_4_reject():
    c, cl, an = setup(); a = run(c, cl, 4)
    assert a["status"] == "REJECTED" and "дисквалификация" in a["decision"]["reasons"][0]


def test_5_manual_review_analyst_sees_factors():
    c, cl, an = setup(); a = run(c, cl, 5)
    assert a["status"] == "MANUAL_REVIEW" and a["sla_due"]
    q = sv.list_applications(c, an); assert q[0]["id"] == a["id"]
    full = sv.get_application(c, an, a["id"])
    assert full["decision"]["grade"] == "B" and len(full["decision"]["factors"]) == 7
    try: sv.analyst_decide(c, an, a["id"], "approve", "  "); assert False
    except ValueError: pass
    r = sv.analyst_decide(c, an, a["id"], "approve", "Стабильный оборот, одобряю")
    assert r["status"] == "APPROVED" and r["decision"]["manual"]["by"] == "analyst"
    assert sv.stats(c, an)["total"] == 1


def test_analyst_request_info_loops_back():
    c, cl, an = setup(); a = run(c, cl, 5)
    r = sv.analyst_decide(c, an, a["id"], "request_info", "Приложите данные по залогу")
    assert r["status"] == "NEEDS_INFO"
    r = sv.provide_info(c, cl, a["id"], {"has_collateral": True, "note": "Залог: оборудование"})
    assert r["status"] in ("MANUAL_REVIEW", "APPROVED", "COUNTER_OFFER")


def test_security_and_integrity():
    c, cl, an = setup(); a = run(c, cl, 1)
    for f in (lambda: sv.respond(c, an, a["id"], True), lambda: sv.stats(c, cl)):
        try: f(); assert False
        except sv.Forbidden: pass
    try: sv.sign(c, cl, a["id"]); assert False          # нельзя подписать до принятия
    except InvalidTransition: pass
    try: sv.respond(c, cl, a["id"], True, expected_version=999); assert False
    except sv.Conflict: pass
    k = sv.create_application(c, cl, "7701234501", "overdraft", 100000, 6, idem_key="k1")
    assert sv.create_application(c, cl, "7701234501", "overdraft", 100000, 6, idem_key="k1")["id"] == k["id"]
    try: sv.create_application(c, cl, "0000000000", "overdraft", 1, 1); assert False
    except ValueError: pass


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"): f(); print("OK", n)
