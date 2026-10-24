"""Смоделированные внешние сервисы: ЕГРЮЛ/ЕГРИП, ФНС «Прозрачный бизнес», БКИ.
Все данные синтетические. В продакшене заменяются адаптерами к реальным API."""

SOURCES = ["ЕГРЮЛ/ЕГРИП (мок)", "ФНС «Прозрачный бизнес» (мок)", "БКИ (мок)"]

COMPANIES = {
    "7701234501": dict(name="ООО «Ромашка Логистик»", okved="49.41 Грузовые перевозки", director="Иванов И.И.",
        address="г. Москва, ул. Тестовая, 1", company_active=True, director_disqualified=False,
        company_age_years=7, annual_revenue=36_000_000, revenue_trend_pct=18, bki_grade="A",
        overdue_count=0, tax_debt=0, okved_risk="low", existing_monthly_payments=0),
    "7701234502": dict(name="ИП Кузнецов А. А.", okved="47.19 Розничная торговля", director="Кузнецов А.А.",
        address="г. Казань, ул. Демонстрационная, 5", company_active=True, director_disqualified=False,
        company_age_years=2.5, annual_revenue=6_000_000, revenue_trend_pct=6, bki_grade="A",
        overdue_count=0, tax_debt=0, okved_risk="mid", existing_monthly_payments=20_000),
    "7701234503": dict(name="ООО «Тритон Сервис»", okved="43.21 Электромонтажные работы", director="Петрова М.С.",
        address="г. Самара, пр. Синтетический, 12", company_active=True, director_disqualified=False,
        company_age_years=4, annual_revenue=None, revenue_trend_pct=None, bki_grade="A",
        overdue_count=0, tax_debt=0, okved_risk="mid", existing_monthly_payments=0),
    "7701234504": dict(name="ООО «Вектор Строй»", okved="41.20 Строительство зданий", director="Сидоров П.Р.",
        address="г. Тула, ул. Мокова, 7", company_active=True, director_disqualified=True,
        company_age_years=3, annual_revenue=15_000_000, revenue_trend_pct=2, bki_grade="B",
        overdue_count=0, tax_debt=0, okved_risk="high", existing_monthly_payments=0),
    "7701234505": dict(name="ООО «Дельта Фуд»", okved="56.10 Рестораны", director="Орлова Е.В.",
        address="г. Екатеринбург, ул. Условная, 3", company_active=True, director_disqualified=False,
        company_age_years=2, annual_revenue=12_000_000, revenue_trend_pct=3, bki_grade="B",
        overdue_count=1, tax_debt=0, okved_risk="mid", existing_monthly_payments=30_000),
}

# Готовые сценарии для проверки жюри
SCENARIOS = [
    dict(n=1, inn="7701234501", product="working_capital", amount=2_000_000, term_months=12,
         expect="Автоодобрение"),
    dict(n=2, inn="7701234502", product="equipment", amount=4_000_000, term_months=24,
         expect="Встречное предложение (меньше сумма, длиннее срок)"),
    dict(n=3, inn="7701234503", product="working_capital", amount=1_500_000, term_months=12,
         expect="Нужны данные → клиент вводит выручку 18 000 000 и динамику 12 → автоодобрение"),
    dict(n=4, inn="7701234504", product="equipment", amount=3_000_000, term_months=36,
         expect="Автоотказ (дисквалификация руководителя)"),
    dict(n=5, inn="7701234505", product="working_capital", amount=2_000_000, term_months=12,
         expect="Ручной разбор аналитиком"),
]


def enrich(inn: str):
    """Имитация обращения к внешним реестрам. None — компания не найдена."""
    c = COMPANIES.get(inn)
    return None if c is None else {**c, "sources": SOURCES}
