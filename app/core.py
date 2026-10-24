"""Ядро кредитного конвейера: state machine, скоринг, кредитное решение.
Без внешних зависимостей. Все допущения описаны в docs/ASSUMPTIONS.md."""
from dataclasses import dataclass, field
from enum import Enum


class S(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    NEEDS_INFO = "NEEDS_INFO"
    PROCESSING = "PROCESSING"          # проверки + обогащение + скоринг
    MANUAL_REVIEW = "MANUAL_REVIEW"
    COUNTER_OFFER = "COUNTER_OFFER"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    OFFER_ACCEPTED = "OFFER_ACCEPTED"
    SIGNED = "SIGNED"                  # симуляция
    DISBURSED = "DISBURSED"            # симуляция
    CANCELLED = "CANCELLED"


TRANSITIONS = {
    S.DRAFT: {S.SUBMITTED, S.CANCELLED},
    S.SUBMITTED: {S.PROCESSING, S.NEEDS_INFO, S.CANCELLED},
    S.NEEDS_INFO: {S.SUBMITTED, S.CANCELLED},
    S.PROCESSING: {S.APPROVED, S.COUNTER_OFFER, S.MANUAL_REVIEW, S.NEEDS_INFO, S.REJECTED},
    S.MANUAL_REVIEW: {S.APPROVED, S.COUNTER_OFFER, S.NEEDS_INFO, S.REJECTED},
    S.COUNTER_OFFER: {S.OFFER_ACCEPTED, S.REJECTED, S.CANCELLED},
    S.APPROVED: {S.OFFER_ACCEPTED, S.CANCELLED},
    S.OFFER_ACCEPTED: {S.SIGNED, S.CANCELLED},
    S.SIGNED: {S.DISBURSED},
    S.DISBURSED: set(), S.REJECTED: set(), S.CANCELLED: set(),
}


class InvalidTransition(Exception):
    pass


def transition(cur: S, new: S) -> S:
    if new not in TRANSITIONS[cur]:
        raise InvalidTransition(f"{cur.value} -> {new.value} запрещён")
    return new


# ---------- продукты и политика ----------
PRODUCTS = {
    # max_months_revenue: лимит как кратность месячной выручки
    "working_capital": dict(name="Оборотный кредит", base_rate=17.0, max_term=24, max_months_revenue=3.0),
    "equipment": dict(name="Кредит на оборудование", base_rate=15.5, max_term=60, max_months_revenue=6.0),
    "overdraft": dict(name="Овердрафт", base_rate=19.0, max_term=12, max_months_revenue=1.5),
}
RISK_PREMIUM = {"A": 0.0, "B": 2.0, "C": 4.5}
MAX_PAYMENT_SHARE = 0.25   # платёж не более 25% месячной выручки


def annuity(amount: float, rate_pct: float, months: int) -> float:
    r = rate_pct / 100 / 12
    return amount / months if r == 0 else amount * r / (1 - (1 + r) ** -months)


@dataclass
class Application:
    product: str
    amount: float
    term_months: int
    # данные компании (из моков ЕГРЮЛ/ФНС/БКИ + ввод клиента)
    company_active: bool = True
    director_disqualified: bool = False
    company_age_years: float | None = None
    annual_revenue: float | None = None
    revenue_trend_pct: float | None = None   # динамика год к году
    bki_grade: str | None = None             # A..D
    overdue_count: int | None = None
    tax_debt: float = 0.0
    okved_risk: str = "mid"                  # low/mid/high
    existing_monthly_payments: float = 0.0
    has_collateral: bool = False


@dataclass
class Result:
    outcome: str                      # AUTO_APPROVED|COUNTER_OFFER|MANUAL_REVIEW|NEEDS_INFO|REJECTED
    score: int | None = None
    grade: str | None = None
    factors: list = field(default_factory=list)     # (фактор, баллы) — объяснимость
    reasons: list = field(default_factory=list)     # reason codes + текст для клиента
    offer: dict | None = None


REQUIRED = ["company_age_years", "annual_revenue", "revenue_trend_pct", "bki_grade", "overdue_count"]


def missing_fields(a: Application) -> list:
    return [f for f in REQUIRED if getattr(a, f) is None]


def knockouts(a: Application) -> list:
    r = []
    if not a.company_active: r.append(("KO_INACTIVE", "Компания не действует по данным ЕГРЮЛ/ЕГРИП"))
    if a.director_disqualified: r.append(("KO_DISQ", "У руководителя дисквалификация"))
    if a.tax_debt > 0.05 * (a.annual_revenue or 1e18): r.append(("KO_TAXDEBT", "Существенная налоговая задолженность"))
    if (a.overdue_count or 0) >= 3: r.append(("KO_OVERDUE", "Множественные просрочки в БКИ"))
    return r


def score(a: Application):
    f = []
    age = a.company_age_years
    f.append(("Возраст компании", 15 if age >= 3 else 10 if age >= 1 else 4))
    t = a.revenue_trend_pct
    f.append(("Динамика выручки", 20 if t >= 10 else 14 if t >= 0 else 5))
    f.append(("Кредитная история (БКИ)", {"A": 25, "B": 17, "C": 8, "D": 0}[a.bki_grade]))
    f.append(("Просрочки", 10 if a.overdue_count == 0 else 4 if a.overdue_count == 1 else 0))
    m = a.annual_revenue / 12
    load = (a.existing_monthly_payments / m) if m else 1
    f.append(("Долговая нагрузка", 15 if load < 0.1 else 9 if load < 0.2 else 2))
    f.append(("Риск отрасли (ОКВЭД)", {"low": 10, "mid": 6, "high": 2}[a.okved_risk]))
    f.append(("Залог", 5 if a.has_collateral else 0))
    total = sum(p for _, p in f)
    grade = "A" if total >= 75 else "B" if total >= 60 else "C" if total >= 45 else "D"
    return total, grade, f


def decide(a: Application) -> Result:
    miss = missing_fields(a)
    if miss:
        return Result("NEEDS_INFO", reasons=[("MISSING", f"Не хватает данных: {', '.join(miss)}")])
    ko = knockouts(a)
    if ko:
        return Result("REJECTED", reasons=ko)
    total, grade, factors = score(a)
    res = Result("", total, grade, factors)
    if grade == "D":
        res.outcome, res.reasons = "REJECTED", [("LOW_SCORE", "Скоринговый балл ниже порога")]
        return res

    p = PRODUCTS[a.product]
    rate = p["base_rate"] + RISK_PREMIUM[grade]
    monthly_rev = a.annual_revenue / 12
    limit = p["max_months_revenue"] * monthly_rev
    pay_cap = MAX_PAYMENT_SHARE * monthly_rev - a.existing_monthly_payments
    term = min(a.term_months, p["max_term"])
    amount = a.amount
    pay = annuity(amount, rate, term)

    if amount <= limit and pay <= pay_cap and term == a.term_months:
        res.outcome = "AUTO_APPROVED" if grade == "A" else "MANUAL_REVIEW"
        res.offer = _offer(amount, term, rate, pay)
        if grade != "A":
            res.reasons.append(("GRADE_" + grade, "Грейд требует решения аналитика"))
        return res

    # встречное предложение: сначала удлиняем срок, потом снижаем сумму
    amount = min(amount, limit)
    for t in range(term, p["max_term"] + 1):
        if annuity(amount, rate, t) <= pay_cap:
            term = t; break
    else:
        term = p["max_term"]
        r = rate / 100 / 12
        k = r / (1 - (1 + r) ** -term) if r else 1 / term
        amount = max(0, min(amount, pay_cap / k))
    if amount < 0.3 * a.amount:
        res.outcome, res.reasons = "REJECTED", [("CAPACITY", "Платёжеспособности недостаточно даже для сниженной суммы")]
        return res
    amount = round(amount, -3)
    res.outcome = "COUNTER_OFFER"
    res.offer = _offer(amount, term, rate, annuity(amount, rate, term))
    res.reasons.append(("COUNTER", "Запрошенные условия превышают лимит или допустимый платёж"))
    return res


def _offer(amount, term, rate, pay):
    return dict(amount=round(amount), term_months=term, rate=round(rate, 2), monthly_payment=round(pay))
