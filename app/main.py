"""HTTP-слой (FastAPI). Вся логика — в app/service.py."""
import os
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import mocks, service as sv
from .core import PRODUCTS, TRANSITIONS, InvalidTransition

DB_PATH = os.getenv("DB_PATH", "data/app.db")
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


@asynccontextmanager
async def lifespan(app):
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sv.connect(DB_PATH); sv.seed(c); c.close()
    yield


app = FastAPI(title="Альфа Конвейер — умный кредитный кабинет", version="1.0.0",
              description="Прототип цифрового кредитного конвейера для МСБ (Альфа-Хак 2026). "
                          "Внешние сервисы смоделированы. Тестовые учётки: client/client123, analyst/analyst123.",
              lifespan=lifespan)


# ---------- схемы ----------
class LoginIn(BaseModel):
    login: str
    password: str

class ApplicationIn(BaseModel):
    inn: str = Field(examples=["7701234501"])
    product: Literal["working_capital", "equipment", "overdraft"]
    amount: float = Field(gt=0, examples=[2000000])
    term_months: int = Field(gt=0, examples=[12])
    purpose: str = ""

class InfoIn(BaseModel):
    annual_revenue: Optional[float] = None
    revenue_trend_pct: Optional[float] = None
    has_collateral: Optional[bool] = None
    existing_monthly_payments: Optional[float] = None
    note: Optional[str] = None

class RespondIn(BaseModel):
    accept: bool

class AnalystIn(BaseModel):
    action: Literal["approve", "reject", "request_info", "counter"]
    comment: str
    offer: Optional[dict] = Field(default=None, description="amount, term_months, rate, monthly_payment (для counter)")


# ---------- зависимости и ошибки ----------
def db():
    c = sv.connect(DB_PATH)
    try:
        yield c
    finally:
        c.close()

def current_user(authorization: Optional[str] = Header(None), conn=Depends(db)):
    token = authorization.split(" ", 1)[1] if authorization and authorization.startswith("Bearer ") else None
    u = sv.user_by_token(conn, token)
    if not u:
        raise sv.Forbidden("Требуется вход")
    return u

def _err(status):
    return lambda request, exc: JSONResponse({"detail": str(exc)}, status_code=status)

for exc, code in [(sv.Forbidden, 403), (sv.NotFound, 404), (sv.Conflict, 409), (InvalidTransition, 409), (ValueError, 422)]:
    app.add_exception_handler(exc, _err(code))


# ---------- API ----------
@app.get("/health", tags=["system"])
def health(conn=Depends(db)):
    conn.execute("SELECT 1"); return {"status": "ok"}

@app.post("/api/auth/login", tags=["auth"])
def login(body: LoginIn, conn=Depends(db)):
    return sv.login(conn, body.login, body.password)

@app.get("/api/me", tags=["auth"])
def me(user=Depends(current_user)):
    return user

@app.get("/api/products", tags=["reference"])
def products():
    return [{"code": k, **v} for k, v in PRODUCTS.items()]

@app.get("/api/demo-scenarios", tags=["reference"], summary="Тестовые сценарии для проверки")
def demo_scenarios():
    return {"scenarios": mocks.SCENARIOS, "companies": {k: v["name"] for k, v in mocks.COMPANIES.items()}}

@app.get("/api/state-machine", tags=["reference"], summary="Допустимые переходы статусов")
def state_machine():
    return {k.value: sorted(x.value for x in v) for k, v in TRANSITIONS.items()}

@app.get("/api/company/{inn}", tags=["reference"], summary="Автозаполнение по ИНН (мок ЕГРЮЛ/ФНС)")
def company(inn: str, user=Depends(current_user)):
    c = mocks.enrich(inn)
    if not c:
        raise sv.NotFound("ИНН не найден в реестрах")
    return {k: c[k] for k in ("name", "okved", "director", "address")}

@app.post("/api/applications", status_code=201, tags=["client"])
def create(body: ApplicationIn, idempotency_key: Optional[str] = Header(None), user=Depends(current_user), conn=Depends(db)):
    return sv.create_application(conn, user, body.inn, body.product, body.amount, body.term_months, body.purpose, idempotency_key)

@app.get("/api/applications", tags=["client", "analyst"])
def list_(user=Depends(current_user), conn=Depends(db)):
    return sv.list_applications(conn, user)

@app.get("/api/applications/{app_id}", tags=["client", "analyst"])
def get(app_id: int, user=Depends(current_user), conn=Depends(db)):
    return sv.get_application(conn, user, app_id)

@app.post("/api/applications/{app_id}/submit", tags=["client"])
def submit(app_id: int, if_match: Optional[int] = Header(None), user=Depends(current_user), conn=Depends(db)):
    return sv.submit(conn, user, app_id, if_match)

@app.post("/api/applications/{app_id}/info", tags=["client"])
def info(app_id: int, body: InfoIn, if_match: Optional[int] = Header(None), user=Depends(current_user), conn=Depends(db)):
    return sv.provide_info(conn, user, app_id, body.model_dump(), if_match)

@app.post("/api/applications/{app_id}/respond", tags=["client"])
def respond(app_id: int, body: RespondIn, if_match: Optional[int] = Header(None), user=Depends(current_user), conn=Depends(db)):
    return sv.respond(conn, user, app_id, body.accept, if_match)

@app.post("/api/applications/{app_id}/sign", tags=["client"], summary="Подписание и выдача (симуляция)")
def sign(app_id: int, if_match: Optional[int] = Header(None), user=Depends(current_user), conn=Depends(db)):
    return sv.sign(conn, user, app_id, if_match)

@app.post("/api/applications/{app_id}/cancel", tags=["client"])
def cancel(app_id: int, if_match: Optional[int] = Header(None), user=Depends(current_user), conn=Depends(db)):
    return sv.cancel(conn, user, app_id, if_match)

@app.post("/api/applications/{app_id}/analyst-decision", tags=["analyst"])
def analyst_decision(app_id: int, body: AnalystIn, if_match: Optional[int] = Header(None), user=Depends(current_user), conn=Depends(db)):
    return sv.analyst_decide(conn, user, app_id, body.action, body.comment, body.offer, if_match)

@app.get("/api/analyst/stats", tags=["analyst"])
def stats(user=Depends(current_user), conn=Depends(db)):
    return sv.stats(conn, user)


# ---------- фронтенд ----------
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", include_in_schema=False)
def index():
    p = os.path.join(STATIC_DIR, "index.html")
    return FileResponse(p) if os.path.exists(p) else RedirectResponse("/docs")
