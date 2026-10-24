"""Бизнес-логика конвейера: БД (SQLite), заявки, роли, журнал событий.
Не зависит от веб-фреймворка — тестируется напрямую."""
import hashlib, json, secrets, sqlite3
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime, timedelta, timezone

from . import mocks
from .core import (PRODUCTS, S, Application, InvalidTransition, decide, missing_fields, transition)

SLA_HOURS = 4


class Conflict(Exception): pass
class Forbidden(Exception): pass
class NotFound(Exception): pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, login TEXT UNIQUE, pw_hash TEXT, salt TEXT, role TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS applications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, inn TEXT, company_name TEXT, product TEXT,
  amount REAL, term_months INTEGER, purpose TEXT, status TEXT, version INTEGER DEFAULT 1,
  extra TEXT DEFAULT '{}', decision TEXT, idem_key TEXT UNIQUE, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER, ts TEXT, actor TEXT, type TEXT,
  from_status TEXT, to_status TEXT, payload TEXT);
CREATE INDEX IF NOT EXISTS ix_app_status ON applications(status);
CREATE INDEX IF NOT EXISTS ix_ev_app ON events(app_id);
"""

STATUS_UI = {
    "DRAFT": ("Черновик", "Проверьте данные и отправьте заявку в банк.", "submit"),
    "SUBMITTED": ("Заявка отправлена", "Банк принял заявку и приступает к проверкам.", None),
    "PROCESSING": ("Проверяем данные", "Идут автоматические проверки и оценка. Обычно это занимает несколько секунд.", None),
    "NEEDS_INFO": ("Нужна дополнительная информация", "Заполните запрошенные данные, и рассмотрение продолжится.", "provide_info"),
    "MANUAL_REVIEW": ("На рассмотрении у специалиста банка", "Решение принимает специалист. Ничего делать не нужно, статус обновится сам.", None),
    "COUNTER_OFFER": ("Банк предлагает другие условия", "Сумма или срок отличаются от запрошенных. Примите предложение или отклоните его.", "respond"),
    "APPROVED": ("Заявка одобрена", "Ознакомьтесь с условиями и примите предложение.", "respond"),
    "REJECTED": ("В финансировании отказано", "Причины указаны ниже. Можно подать новую заявку с другими параметрами.", None),
    "OFFER_ACCEPTED": ("Предложение принято", "Подпишите документы, чтобы получить финансирование.", "sign"),
    "SIGNED": ("Документы подписаны", "Банк готовит перечисление средств.", None),
    "DISBURSED": ("Средства выданы", "Деньги зачислены на счёт компании. Подписание и выдача смоделированы в рамках демо.", None),
    "CANCELLED": ("Заявка отменена", "Вы можете создать новую заявку.", None),
}
EVENT_LABELS = {
    "CREATED": "Черновик создан", "SUBMITTED": "Заявка отправлена в банк",
    "PROCESSING_STARTED": "Запущены проверки и скоринг", "ENRICHMENT": "Данные о компании получены из реестров",
    "DECISION": "Система сформировала результат", "INFO_PROVIDED": "Вы предоставили данные",
    "ANALYST_DECISION": "Решение специалиста банка", "OFFER_ACCEPTED": "Вы приняли предложение",
    "OFFER_DECLINED": "Вы отклонили предложение", "SIGNED": "Документы подписаны (симуляция)",
    "DISBURSED": "Средства выданы (симуляция)", "CANCELLED": "Заявка отменена",
}
FIELD_LABELS = {
    "annual_revenue": "Годовая выручка, ₽", "revenue_trend_pct": "Динамика выручки к прошлому году, %",
    "company_age_years": "Возраст компании, лет", "bki_grade": "Кредитный рейтинг БКИ",
    "overdue_count": "Количество просрочек",
}
APP_FIELDS = {f.name for f in fields(Application)} - {"product", "amount", "term_months"}
CLIENT_EDITABLE = {"annual_revenue", "revenue_trend_pct", "has_collateral", "existing_monthly_payments"}
OUTCOME_STATUS = {"AUTO_APPROVED": S.APPROVED, "COUNTER_OFFER": S.COUNTER_OFFER,
                  "MANUAL_REVIEW": S.MANUAL_REVIEW, "NEEDS_INFO": S.NEEDS_INFO, "REJECTED": S.REJECTED}


# ---------- инфраструктура ----------
def connect(path=":memory:"):
    c = sqlite3.connect(path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    if path != ":memory:":
        c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


@contextmanager
def tx(conn):
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def seed(conn):
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return
    with tx(conn):
        for login, pw, role, name in [("client", "client123", "client", "Предприниматель (демо)"),
                                      ("analyst", "analyst123", "analyst", "Кредитный аналитик (демо)")]:
            salt = secrets.token_hex(8)
            conn.execute("INSERT INTO users(login,pw_hash,salt,role,name) VALUES(?,?,?,?,?)",
                         (login, _hash(pw, salt), salt, role, name))


# ---------- аутентификация ----------
def login(conn, login_, password):
    u = conn.execute("SELECT * FROM users WHERE login=?", (login_,)).fetchone()
    if not u or not secrets.compare_digest(u["pw_hash"], _hash(password, u["salt"])):
        raise Forbidden("Неверный логин или пароль")
    token = secrets.token_hex(24)
    with tx(conn):
        conn.execute("INSERT INTO sessions VALUES(?,?,?)", (token, u["id"], _now()))
    return {"token": token, "user": _user(u)}


def _user(u):
    return {"id": u["id"], "login": u["login"], "role": u["role"], "name": u["name"]}


def user_by_token(conn, token):
    if not token:
        return None
    u = conn.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (token,)).fetchone()
    return _user(u) if u else None


# ---------- внутренние помощники ----------
def _load(conn, app_id):
    r = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not r:
        raise NotFound(f"Заявка {app_id} не найдена")
    return r


def _own(user, row):
    if user["role"] != "client" or row["owner_id"] != user["id"]:
        raise Forbidden("Действие доступно только владельцу заявки")


def _access(user, row):
    if user["role"] == "analyst" or row["owner_id"] == user["id"]:
        return
    raise Forbidden("Нет доступа к заявке")


def _event(conn, app_id, actor, type_, frm=None, to=None, payload=None):
    conn.execute("INSERT INTO events(app_id,ts,actor,type,from_status,to_status,payload) VALUES(?,?,?,?,?,?,?)",
                 (app_id, _now(), actor, type_, frm, to, json.dumps(payload or {}, ensure_ascii=False)))


def _move(conn, app_id, new, actor, type_, payload=None, expected_version=None):
    row = _load(conn, app_id)
    if expected_version is not None and expected_version != row["version"]:
        raise Conflict("Заявка была изменена; обновите страницу")
    new = transition(S(row["status"]), new)           # InvalidTransition при нарушении
    cur = conn.execute("UPDATE applications SET status=?, version=version+1, updated_at=? WHERE id=? AND version=?",
                       (new.value, _now(), app_id, row["version"]))
    if cur.rowcount == 0:
        raise Conflict("Конкурентное изменение заявки")
    _event(conn, app_id, actor, type_, row["status"], new.value, payload)


def _set_decision(conn, app_id, decision):
    conn.execute("UPDATE applications SET decision=? WHERE id=?", (json.dumps(decision, ensure_ascii=False), app_id))


def _process(conn, app_id):
    """PROCESSING: обогащение → проверки → скоринг → кредитное решение."""
    row = _load(conn, app_id)
    _move(conn, app_id, S.PROCESSING, "system", "PROCESSING_STARTED")
    en = mocks.enrich(row["inn"])
    _event(conn, app_id, "system", "ENRICHMENT", payload={
        "sources": en["sources"], "company": {k: en[k] for k in ("name", "okved", "director", "address")}})
    data = {k: v for k, v in en.items() if k in APP_FIELDS}
    data.update({k: v for k, v in json.loads(row["extra"]).items() if k in APP_FIELDS and v is not None})
    a = Application(product=row["product"], amount=row["amount"], term_months=row["term_months"], **data)
    res = decide(a)
    decision = {"outcome": res.outcome, "score": res.score, "grade": res.grade,
                "factors": [list(f) for f in res.factors], "reasons": [list(r) for r in res.reasons],
                "offer": res.offer, "auto": res.outcome == "AUTO_APPROVED",
                "requested": [{"field": f, "label": FIELD_LABELS.get(f, f)} for f in missing_fields(a)]
                             if res.outcome == "NEEDS_INFO" else []}
    _set_decision(conn, app_id, decision)
    _move(conn, app_id, OUTCOME_STATUS[res.outcome], "system", "DECISION",
          {"outcome": res.outcome, "score": res.score, "grade": res.grade})


# ---------- сценарии клиента ----------
def create_application(conn, user, inn, product, amount, term_months, purpose="", idem_key=None):
    if user["role"] != "client":
        raise Forbidden("Заявки создают клиенты")
    if product not in PRODUCTS:
        raise ValueError("Неизвестный продукт")
    if not (inn.isdigit() and len(inn) in (10, 12)):
        raise ValueError("ИНН должен состоять из 10 или 12 цифр")
    if amount <= 0 or not (1 <= term_months <= PRODUCTS[product]["max_term"]):
        raise ValueError(f"Срок для этого продукта: 1–{PRODUCTS[product]['max_term']} мес., сумма больше 0")
    company = mocks.enrich(inn)
    if company is None:
        raise ValueError("ИНН не найден в реестрах (демо: используйте ИНН из тестовых сценариев)")
    if idem_key:
        ex = conn.execute("SELECT id FROM applications WHERE idem_key=? AND owner_id=?", (idem_key, user["id"])).fetchone()
        if ex:
            return get_application(conn, user, ex["id"])
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO applications(owner_id,inn,company_name,product,amount,term_months,purpose,status,idem_key,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (user["id"], inn, company["name"], product, amount, term_months, purpose, S.DRAFT.value, idem_key, _now(), _now()))
        app_id = cur.lastrowid
        _event(conn, app_id, "client", "CREATED", None, S.DRAFT.value, {"product": product, "amount": amount})
    return get_application(conn, user, app_id)


def submit(conn, user, app_id, expected_version=None):
    _own(user, _load(conn, app_id))
    with tx(conn):
        _move(conn, app_id, S.SUBMITTED, "client", "SUBMITTED", expected_version=expected_version)
        _process(conn, app_id)
    return get_application(conn, user, app_id)


def provide_info(conn, user, app_id, data, expected_version=None):
    row = _load(conn, app_id)
    _own(user, row)
    clean = {k: v for k, v in data.items() if k in CLIENT_EDITABLE and v is not None}
    note = data.get("note")
    if not clean and not note:
        raise ValueError("Не переданы данные")
    extra = json.loads(row["extra"])
    extra.update(clean)
    with tx(conn):
        conn.execute("UPDATE applications SET extra=? WHERE id=?", (json.dumps(extra), app_id))
        _move(conn, app_id, S.SUBMITTED, "client", "INFO_PROVIDED",
              {"fields": list(clean), "note": note}, expected_version=expected_version)
        _process(conn, app_id)
    return get_application(conn, user, app_id)


def respond(conn, user, app_id, accept, expected_version=None):
    _own(user, _load(conn, app_id))
    with tx(conn):
        if accept:
            _move(conn, app_id, S.OFFER_ACCEPTED, "client", "OFFER_ACCEPTED", expected_version=expected_version)
        else:
            _move(conn, app_id, S.CANCELLED, "client", "OFFER_DECLINED", expected_version=expected_version)
    return get_application(conn, user, app_id)


def sign(conn, user, app_id, expected_version=None):
    """Симуляция: подписание и выдача без реальных операций."""
    row = _load(conn, app_id)
    _own(user, row)
    offer = (json.loads(row["decision"] or "{}")).get("offer")
    with tx(conn):
        _move(conn, app_id, S.SIGNED, "client", "SIGNED", {"simulated": True}, expected_version=expected_version)
        _move(conn, app_id, S.DISBURSED, "system", "DISBURSED", {"simulated": True, "offer": offer})
    return get_application(conn, user, app_id)


def cancel(conn, user, app_id, expected_version=None):
    _own(user, _load(conn, app_id))
    with tx(conn):
        _move(conn, app_id, S.CANCELLED, "client", "CANCELLED", expected_version=expected_version)
    return get_application(conn, user, app_id)


# ---------- сценарии аналитика ----------
def analyst_decide(conn, user, app_id, action, comment, offer=None, expected_version=None):
    if user["role"] != "analyst":
        raise Forbidden("Только для сотрудников банка")
    if not (comment or "").strip():
        raise ValueError("Комментарий к решению обязателен")
    target = {"approve": S.APPROVED, "reject": S.REJECTED, "request_info": S.NEEDS_INFO, "counter": S.COUNTER_OFFER}.get(action)
    if target is None:
        raise ValueError("Неизвестное действие")
    row = _load(conn, app_id)
    dec = json.loads(row["decision"] or "{}")
    if action == "counter":
        if not offer:
            raise ValueError("Для встречного предложения укажите условия")
        dec["offer"] = offer
    if action == "request_info":
        dec["requested"] = [{"field": "note", "label": comment.strip()}]
    dec["manual"] = {"by": user["login"], "action": action, "comment": comment.strip(), "at": _now()}
    with tx(conn):
        _set_decision(conn, app_id, dec)
        _move(conn, app_id, target, "analyst", "ANALYST_DECISION",
              {"action": action, "comment": comment.strip(), "by": user["login"]}, expected_version=expected_version)
    return get_application(conn, user, app_id)


def stats(conn, user):
    if user["role"] != "analyst":
        raise Forbidden("Только для сотрудников банка")
    by = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM applications GROUP BY status")}
    total = sum(by.values())
    decided = sum(by.get(k, 0) for k in ("APPROVED", "OFFER_ACCEPTED", "SIGNED", "DISBURSED", "COUNTER_OFFER", "REJECTED"))
    positive = decided - by.get("REJECTED", 0)
    return {"total": total, "by_status": by, "approval_rate": round(positive / decided, 2) if decided else None}


# ---------- представления ----------
def _view(row, events, user):
    dec = json.loads(row["decision"]) if row["decision"] else None
    title, hint, action = STATUS_UI[row["status"]]
    is_analyst = user["role"] == "analyst"
    if dec and not is_analyst:
        dec = {k: dec.get(k) for k in ("outcome", "reasons", "offer", "requested", "auto")}
        dec["reasons"] = [r[1] for r in (dec["reasons"] or [])]
    sla = None
    if row["status"] == "MANUAL_REVIEW":
        sla = (datetime.fromisoformat(row["updated_at"]) + timedelta(hours=SLA_HOURS)).isoformat(timespec="seconds")
    return {
        "id": row["id"], "inn": row["inn"], "company_name": row["company_name"], "product": row["product"],
        "product_name": PRODUCTS[row["product"]]["name"], "amount": row["amount"], "term_months": row["term_months"],
        "purpose": row["purpose"], "status": row["status"], "version": row["version"],
        "status_title": title, "status_hint": hint, "next_action": action if not is_analyst else None,
        "decision": dec, "sla_due": sla, "created_at": row["created_at"], "updated_at": row["updated_at"],
        "events": [{"ts": e["ts"], "type": e["type"], "label": EVENT_LABELS.get(e["type"], e["type"]),
                    "actor": e["actor"], "from": e["from_status"], "to": e["to_status"],
                    **({"payload": json.loads(e["payload"])} if is_analyst else {})} for e in events],
    }


def get_application(conn, user, app_id):
    row = _load(conn, app_id)
    _access(user, row)
    ev = conn.execute("SELECT * FROM events WHERE app_id=? ORDER BY id", (app_id,)).fetchall()
    return _view(row, ev, user)


def list_applications(conn, user):
    if user["role"] == "analyst":
        rows = conn.execute("SELECT * FROM applications WHERE status!='DRAFT' ORDER BY (status='MANUAL_REVIEW') DESC, updated_at DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM applications WHERE owner_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    out = []
    for r in rows:
        v = _view(r, [], user)
        v.pop("events")
        out.append(v)
    return out
