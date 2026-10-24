# Clinic Appointment Booking API

A REST API where clinics publish, list and cancel appointment slots, and patients book
and cancel bookings. The main guarantee is that **a slot can never have more than one
active booking**, even when many booking requests arrive at the same moment.

Built with FastAPI, SQLAlchemy 2, Pydantic 2 and Alembic. Runs on SQLite with no setup
and on PostgreSQL for production-like concurrency behaviour.

**Scope:** this is an assignment-sized service. It has no authentication or
authorisation, no clinic or patient registry, and no notifications. Anyone who can reach
the API can create, view and cancel any slot or booking (including patient names and
emails). Do not expose it publicly as-is; see [Limitations](#known-limitations).

## Requirements

- Python 3.11+ (tested locally and in CI on 3.11 and 3.13)
- [uv](https://docs.astral.sh/uv/) for the locked, reproducible install (`uv.lock`)
- Optional: PostgreSQL 16 (or Docker) for the PostgreSQL test path

## Setup from a fresh clone

```bash
git clone https://github.com/adi-tya10/clinic-appointment-booking-api.git
cd clinic-appointment-booking-api
uv sync --locked --extra dev      # creates .venv with the exact locked versions
cp .env.example .env              # optional; defaults work without it
uv run alembic upgrade head       # creates the schema (SQLite file ./clinic.db by default)
uv run uvicorn clinic_booking.main:create_app --factory --port 8000
```

Without uv: `python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`,
then run the same `alembic` / `uvicorn` / `pytest` commands without the `uv run` prefix
(versions are then resolved from the ranges in `pyproject.toml`, not the lock file).

- Base URL: `http://127.0.0.1:8000`
- Interactive OpenAPI docs: `http://127.0.0.1:8000/docs` (spec at `/openapi.json`)

### Configuration

Read from environment variables (or a `.env` file):

| Variable       | Default                  | Notes |
|----------------|--------------------------|-------|
| `DATABASE_URL` | `sqlite:///./clinic.db`  | PostgreSQL: `postgresql+psycopg://user:password@host:5432/dbname` |
| `LOG_LEVEL`    | `INFO`                   | Logs record ids and outcomes only, never patient names or emails. |

`.env` is git-ignored; `.env.example` contains no real credentials.

### Using PostgreSQL

```bash
docker compose up -d postgres     # creates databases "clinic" and "clinic_test"
export DATABASE_URL=postgresql+psycopg://clinic:clinic@localhost:5432/clinic
uv run alembic upgrade head
uv run uvicorn clinic_booking.main:create_app --factory --port 8000
```

The compose credentials are throwaway local defaults. The schema is managed with
Alembic (`migrations/`); the app never creates tables itself.

## Conventions

- **Time:** all timestamps are ISO 8601 and **must include a UTC offset**
  (`2026-11-02T09:00:00+01:00` or `...Z`). Naive timestamps are rejected. Values are
  stored and returned in UTC (`2026-11-02T08:00:00Z`).
- **Ids:** positive integers. Non-numeric, zero, negative or out-of-range ids → 422.
- **Unknown fields** in request bodies are rejected (422) so typos don't pass silently.
- **Errors** always have the same shape:

  ```json
  {"error": {"code": "slot_already_booked", "message": "Slot 1 is already booked."}}
  ```

  Validation errors add `details`: `[{"location": "body.start_at", "message": "..."}]`.
  Submitted values are never echoed back, and internal errors never expose SQL,
  stack traces or exception text.

| Status | Meaning |
|--------|---------|
| 200 / 201 | Success. Creation returns 201 with the resource and a `Location` header. |
| 404 | Slot / booking / route does not exist (`slot_not_found`, `booking_not_found`, `not_found`). |
| 405 | Method not allowed on that path. |
| 409 | Valid request that conflicts with current state (see codes below). |
| 422 | Malformed JSON, invalid fields, invalid ids or filters (`validation_error`). |
| 500 | Unexpected failure (`internal_error`), generic message only. |
| 503 | Database unavailable or locked (`database_unavailable`); safe to retry. |

## State model

```
Slot:     available ──book──▶ booked ──cancel booking──▶ available
          available ──cancel slot──▶ cancelled   (terminal)
Booking:  active ──cancel──▶ cancelled           (terminal, record kept)
```

Business rules:

- A slot is bookable only while `available` and its `start_at` is in the future
  (`slot_already_booked`, `slot_cancelled`, `slot_already_started` otherwise).
- **Cancelling a slot that has an active booking is rejected** with 409
  `slot_has_active_booking`. Cancel the booking first; this way no patient is ever left
  holding a booking for a withdrawn slot.
- Cancelling a booking keeps the row (`status: cancelled`, `cancelled_at` set) and makes
  the slot `available` again in the same transaction, so it can be rebooked.
- Repeating a cancellation returns 409 (`booking_already_cancelled`,
  `slot_already_cancelled`) and changes nothing. A duplicate booking request (same
  patient, same slot) gets 409 `slot_already_booked`.
- **Overlapping slots for the same clinic are allowed.** A clinic usually has several
  practitioners or rooms, and the API doesn't model those, so it can't tell an
  intentional parallel slot from a mistake. Rejecting overlaps would block legitimate
  schedules.
- Slot validation: `end_at` after `start_at`, `start_at` in the future, max 24 hours,
  `clinic_id` 1–64 characters of `A-Z a-z 0-9 - _`.

## Endpoints

| Method | Path | Purpose | Statuses |
|--------|------|---------|----------|
| POST | `/slots` | Publish a slot | 201, 422 |
| GET | `/slots` | List slots. Filters: `clinic_id`, `status`, `starts_from`, `starts_before`, `limit` (1–200, default 50), `offset` | 200, 422 |
| GET | `/slots/{slot_id}` | Get a slot | 200, 404, 422 |
| POST | `/slots/{slot_id}/cancel` | Cancel an available slot | 200, 404, 409, 422 |
| POST | `/slots/{slot_id}/bookings` | Book the slot for a patient | 201, 404, 409, 422 |
| GET | `/bookings` | List bookings. Filters: `slot_id`, `status`, `limit`, `offset` | 200, 422 |
| GET | `/bookings/{booking_id}` | Get a booking | 200, 404, 422 |
| POST | `/bookings/{booking_id}/cancel` | Cancel a booking and free its slot | 200, 404, 409, 422 |
| GET | `/health` | API and database reachability | 200, 503 |

Cancellation is `POST .../cancel` rather than `DELETE`, because nothing is deleted:
it's a state transition that returns the updated resource.

## Examples

```bash
# Publish a slot (times in any offset; stored in UTC)
curl -i -X POST http://127.0.0.1:8000/slots \
  -H 'Content-Type: application/json' \
  -d '{"clinic_id": "riverside-clinic", "start_at": "2026-11-02T09:00:00+01:00", "end_at": "2026-11-02T09:30:00+01:00"}'
```
```
HTTP/1.1 201 Created
location: /slots/1

{"id":1,"clinic_id":"riverside-clinic","start_at":"2026-11-02T08:00:00Z","end_at":"2026-11-02T08:30:00Z","status":"available","created_at":"2026-10-24T13:36:50.913853Z","updated_at":"2026-10-24T13:36:50.913859Z"}
```

```bash
# Upcoming available slots for one clinic
curl 'http://127.0.0.1:8000/slots?clinic_id=riverside-clinic&status=available&starts_from=2026-10-24T00:00:00Z'
```
```json
{"items":[{"id":1,"clinic_id":"riverside-clinic","start_at":"2026-11-02T08:00:00Z","end_at":"2026-11-02T08:30:00Z","status":"available","created_at":"2026-10-24T13:36:50.913853Z","updated_at":"2026-10-24T13:36:50.913859Z"}],"limit":50,"offset":0}
```

```bash
# Book it
curl -i -X POST http://127.0.0.1:8000/slots/1/bookings \
  -H 'Content-Type: application/json' \
  -d '{"patient_name": "Asha Rao", "patient_email": "asha.rao@example.com"}'
```
```
HTTP/1.1 201 Created
location: /bookings/1

{"id":1,"slot_id":1,"patient_name":"Asha Rao","patient_email":"asha.rao@example.com","status":"active","created_at":"2026-10-24T13:36:50.953728Z","cancelled_at":null}
```

```bash
# A second patient tries the same slot → 409
curl -X POST http://127.0.0.1:8000/slots/1/bookings \
  -H 'Content-Type: application/json' \
  -d '{"patient_name": "Ben Ortiz", "patient_email": "ben.ortiz@example.com"}'
# {"error":{"code":"slot_already_booked","message":"Slot 1 is already booked."}}

# The clinic can't withdraw a booked slot → 409
curl -X POST http://127.0.0.1:8000/slots/1/cancel
# {"error":{"code":"slot_has_active_booking","message":"Slot 1 has an active booking. Cancel the booking before cancelling the slot."}}

# Cancel the booking (slot becomes available again); repeating it → 409
curl -X POST http://127.0.0.1:8000/bookings/1/cancel
# {"id":1,"slot_id":1,...,"status":"cancelled","cancelled_at":"2026-10-24T13:36:50.993846Z"}
curl -X POST http://127.0.0.1:8000/bookings/1/cancel
# {"error":{"code":"booking_already_cancelled","message":"Booking 1 is already cancelled."}}

# Booking history for a slot
curl 'http://127.0.0.1:8000/bookings?slot_id=1'

# Validation error (missing UTC offset)
curl -X POST http://127.0.0.1:8000/slots -H 'Content-Type: application/json' \
  -d '{"clinic_id": "riverside-clinic", "start_at": "2026-11-02T09:30:00", "end_at": "2026-11-02T09:00:00+01:00"}'
# 422 {"error":{"code":"validation_error","message":"The request is invalid.","details":[{"location":"body.start_at","message":"Input should have timezone info"}]}}

# Missing resource
curl http://127.0.0.1:8000/bookings/99
# 404 {"error":{"code":"booking_not_found","message":"Booking 99 does not exist."}}
```

## How double-booking is prevented

Checking availability with a `SELECT` and then inserting a booking is a race: two
requests can both see "available" before either writes. Instead, booking
(`clinic_booking/services/bookings.py`) runs **one transaction**:

1. `UPDATE slots SET status='booked' WHERE id=:id AND status='available' AND start_at > :now`
2. Only if that updated exactly one row: `INSERT` the booking.
3. Commit. Any error rolls back both statements.

The check and the write are a single statement, so the database decides who wins. On
PostgreSQL, a concurrent transaction targeting the same row waits for the first one's
row lock and then re-evaluates the `WHERE` against the committed row, finding it
`booked` and updating nothing. On SQLite, writers are serialised by the database file
lock. Either way, at most one request claims the slot; every other one gets 409
`slot_already_booked` and writes nothing.

A second, independent guard lives in the schema: a **partial unique index**
`UNIQUE (slot_id) WHERE status = 'active'` on `bookings`. Even if some future code path
skipped the conditional update, the database would refuse a second active booking; the
transaction would roll back (including the slot status change) and the client would get
a clean 409. CHECK constraints also enforce `end_at > start_at`, valid status values,
and that `cancelled_at` is set exactly when a booking is cancelled.

Cancellations use the same pattern (`UPDATE ... WHERE status = <expected>`), so
cancel-vs-book and cancel-vs-cancel races also resolve to exactly one winner. Cancelling
a booking and releasing its slot happen in one transaction, so availability never
disagrees with bookings.

If a conditional update matches nothing, the service reads the slot to report *why*. If
that read finds the slot available again (e.g. a booking was cancelled in between), the
claim is simply retried, up to 3 attempts. The client then gets either a booking or the
real reason, not a vague conflict.

## Running tests

```bash
uv run pytest                     # SQLite: fast, no services needed
uv run ruff check . && uv run ruff format --check .
```

Against PostgreSQL (the whole suite, plus 2 PostgreSQL-only tests):

```bash
docker compose up -d postgres
TEST_DATABASE_URL=postgresql+psycopg://clinic:clinic@localhost:5432/clinic_test uv run pytest
```

`TEST_DATABASE_URL` is **wiped before every test**; never point it at real data.
Without it, each test gets its own temporary SQLite file and the PostgreSQL-only tests
are skipped. Set `REQUIRE_POSTGRES=1` (as CI does) to make that a hard error instead.

What the suite covers (`tests/`):

- `test_slots.py`, `test_bookings.py`: creation, listing and filters, booking,
  conflicts, cancellation, rebooking, repeated cancellation, invalid transitions,
  404s, invalid ids and payloads, and that patient data isn't echoed in errors.
- `test_concurrency.py`: starts a real uvicorn server and fires parallel HTTP
  requests from separate threads, released together by a barrier. Each request uses
  its own DB session/connection. It covers 16 simultaneous bookings for one slot
  (exactly one 201), parallel bookings of different slots (all succeed), slot
  cancellation racing bookings, parallel cancellations of one booking, and booking
  cancellation racing rebooking. After each scenario it checks the invariant: ≤1
  active booking per slot, and a slot is `booked` exactly when it has one. Assertions
  are on outcomes, never timing.
  The PostgreSQL-only test interleaves two transactions deterministically: A holds an
  uncommitted claim, and B must block, then lose (if A commits) or win (if A rolls back).
- `test_integrity.py`: DB constraints enforced directly, and no partial state
  when a transaction fails midway (failures injected into the booking `INSERT` and the
  slot-release `UPDATE`).
- `test_errors.py`: uniform error shape for 404/405/422/500/503, no leaking of
  internals, health check, and the OpenAPI contract.
- `test_migrations.py`: `alembic upgrade head` produces exactly the model schema
  (autogenerate diff is empty), enforces the unique index, and downgrades cleanly.

CI (`.github/workflows/ci.yml`) runs lint, the SQLite suite on Python 3.11 and 3.13,
and the full suite against a PostgreSQL 16 service, using the same commands as above.

## Project layout

```
clinic_booking/
  main.py            app factory, router and error-handler wiring
  config.py          settings from environment
  db.py              engine/session setup, UTC datetime column type
  models.py          Slot, Booking tables and constraints
  schemas.py         request/response models and validation rules
  errors.py          API error types and the JSON error handlers
  services/          business rules and transactions (slots.py, bookings.py)
  routes/            HTTP layer only: parse, call a service, shape the response
migrations/          Alembic environment and the schema migration
tests/
```

Routes stay thin; every state change and transaction boundary lives in `services/`.
Each request gets its own session from a FastAPI dependency, and the session is
always closed. Every write runs in an explicit `with session.begin():` block.

## Known limitations

- **No authentication or authorisation**, and no tenant isolation between clinics.
  Patient emails are visible to any caller of `GET /bookings`. Out of scope for this
  assignment, but required before any real deployment.
- `clinic_id` is a free-form identifier; there is no clinic table to validate it against.
- **SQLite is for ease of evaluation.** It serialises all writers with a file lock,
  so its concurrency results show the application logic is correct, but they don't
  show how PostgreSQL behaves. Use PostgreSQL to validate production-like behaviour.
  Under heavy write contention SQLite can exceed its 30 s lock wait and return 503.
- The "slot is `booked` ⇔ it has an active booking" rule spans two tables and is kept
  by the service transactions (and checked by tests), not by a database trigger. The
  "≤1 active booking per slot" rule *is* database-enforced.
- No idempotency keys: a client that retries a successful booking after a lost
  response gets 409 `slot_already_booked` and should fetch `GET /bookings?slot_id=`.
- No rate limiting, CORS configuration or deployment setup; CORS is not enabled.
