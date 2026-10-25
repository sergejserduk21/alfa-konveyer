# Модель данных

СУБД: SQLite (файл в docker-томе). Схема — в `app/service.py` (`SCHEMA`). Для промышленной эксплуатации предполагается PostgreSQL: схема переносима без изменения логики.

```mermaid
erDiagram
    USERS ||--o{ SESSIONS : "имеет"
    USERS ||--o{ APPLICATIONS : "создаёт (owner_id)"
    APPLICATIONS ||--o{ EVENTS : "журнал переходов"
    USERS {
        int id PK
        text login UK
        text pw_hash "PBKDF2-SHA256, 100k итераций"
        text salt
        text role "client | analyst"
        text name
    }
    SESSIONS {
        text token PK
        int user_id FK
        text created_at
    }
    APPLICATIONS {
        int id PK
        int owner_id FK
        text inn
        text company_name
        text product "working_capital | equipment | overdraft"
        real amount
        int term_months
        text purpose
        text status "см. диаграмму состояний"
        int version "оптимистичная блокировка"
        text extra "JSON: данные, введённые клиентом"
        text decision "JSON: скоринг, факторы, reason codes, оффер"
        text idem_key UK "идемпотентность создания"
        text created_at
        text updated_at
    }
    EVENTS {
        int id PK
        int app_id FK
        text ts
        text actor "client | system | analyst"
        text type
        text from_status
        text to_status
        text payload "JSON"
    }
```

## Принципы
- **Журнал событий (`events`) только добавляется**, записи не изменяются. На нём строятся трекер клиента и аудит аналитика.
- Все изменения статуса и запись события выполняются в одной транзакции (`tx()`): либо оба, либо ни одного.
- Клиент не получает скоринговые баллы и факторы: они отфильтровываются в `_view()` по роли.
- Данные клиентов и компаний — синтетические (`app/mocks.py`).
