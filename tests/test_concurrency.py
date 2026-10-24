"""Race-condition tests.

Requests go through a real uvicorn server (``live_server``), so each one is handled on
its own worker thread with its own database session and connection. A barrier
releases all requests together to maximise overlap; correctness is asserted on final
outcomes, never on timing.
"""

import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from sqlalchemy import text

from clinic_booking.errors import ConflictError
from clinic_booking.models import Booking, Slot, SlotStatus
from clinic_booking.schemas import BookingCreate
from clinic_booking.services.bookings import book_slot
from tests.helpers import assert_booking_invariants, book, booking_payload, create_slot

Call = Callable[[httpx2.Client], httpx2.Response]


def run_concurrently(base_url: str, calls: list[Call]) -> list[httpx2.Response]:
    clients = [httpx2.Client(base_url=base_url, timeout=60) for _ in calls]
    barrier = threading.Barrier(len(calls))

    def run(index: int) -> httpx2.Response:
        barrier.wait()
        return calls[index](clients[index])

    try:
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            return list(pool.map(run, range(len(calls))))
    finally:
        for client in clients:
            client.close()


def booking_call(slot_id: int, n: int) -> Call:
    return lambda client: client.post(f"/slots/{slot_id}/bookings", json=booking_payload(n))


def post_call(path: str) -> Call:
    return lambda client: client.post(path)


def error_code(response: httpx2.Response) -> str:
    return response.json()["error"]["code"]


@pytest.fixture
def api(live_server):
    with httpx2.Client(base_url=live_server, timeout=60) as client:
        yield client


@pytest.mark.parametrize("round_number", range(3))
def test_concurrent_bookings_for_one_slot_exactly_one_succeeds(
    live_server, api, session_factory, round_number
):
    slot = create_slot(api)
    attempts = 16

    responses = run_concurrently(
        live_server, [booking_call(slot["id"], n) for n in range(attempts)]
    )

    statuses = Counter(r.status_code for r in responses)
    assert statuses == {201: 1, 409: attempts - 1}
    assert {error_code(r) for r in responses if r.status_code == 409} == {"slot_already_booked"}
    winner = next(r.json() for r in responses if r.status_code == 201)
    bookings = api.get("/bookings", params={"slot_id": slot["id"]}).json()["items"]
    assert [b["id"] for b in bookings] == [winner["id"]]
    assert api.get(f"/slots/{slot['id']}").json()["status"] == "booked"
    assert_booking_invariants(session_factory)


def test_concurrent_bookings_for_different_slots_all_succeed(live_server, api, session_factory):
    slots = [create_slot(api) for _ in range(10)]

    responses = run_concurrently(
        live_server, [booking_call(slot["id"], n) for n, slot in enumerate(slots)]
    )

    assert [r.status_code for r in responses] == [201] * len(slots)
    assert {r.json()["slot_id"] for r in responses} == {s["id"] for s in slots}
    assert_booking_invariants(session_factory)


@pytest.mark.parametrize("round_number", range(5))
def test_slot_cancellation_racing_bookings_never_orphans_a_booking(
    live_server, api, session_factory, round_number
):
    slot = create_slot(api)
    calls = [post_call(f"/slots/{slot['id']}/cancel")] + [
        booking_call(slot["id"], n) for n in range(6)
    ]

    cancel_response, *booking_responses = run_concurrently(live_server, calls)

    booked = [r for r in booking_responses if r.status_code == 201]
    assert all(r.status_code in (201, 409) for r in booking_responses)
    final_status = api.get(f"/slots/{slot['id']}").json()["status"]
    if cancel_response.status_code == 200:
        assert booked == []
        assert final_status == "cancelled"
    else:
        assert cancel_response.status_code == 409
        assert error_code(cancel_response) == "slot_has_active_booking"
        assert len(booked) == 1
        assert final_status == "booked"
    assert_booking_invariants(session_factory)


def test_concurrent_cancellations_of_one_booking_exactly_one_succeeds(
    live_server, api, session_factory
):
    slot = create_slot(api)
    booking = book(api, slot["id"])

    responses = run_concurrently(
        live_server, [post_call(f"/bookings/{booking['id']}/cancel") for _ in range(8)]
    )

    assert Counter(r.status_code for r in responses) == {200: 1, 409: 7}
    assert {error_code(r) for r in responses if r.status_code == 409} == {
        "booking_already_cancelled"
    }
    assert api.get(f"/slots/{slot['id']}").json()["status"] == "available"
    assert_booking_invariants(session_factory)


@pytest.mark.parametrize("round_number", range(5))
def test_booking_cancellation_racing_rebooking_keeps_invariant(
    live_server, api, session_factory, round_number
):
    slot = create_slot(api)
    booking = book(api, slot["id"], n=100)
    calls = [post_call(f"/bookings/{booking['id']}/cancel")] + [
        booking_call(slot["id"], n) for n in range(8)
    ]

    cancel_response, *booking_responses = run_concurrently(live_server, calls)

    assert cancel_response.status_code == 200
    rebooked = [r for r in booking_responses if r.status_code == 201]
    assert len(rebooked) <= 1
    assert all(
        error_code(r) == "slot_already_booked" for r in booking_responses if r.status_code != 201
    )
    expected = "booked" if rebooked else "available"
    assert api.get(f"/slots/{slot['id']}").json()["status"] == expected
    assert_booking_invariants(session_factory)


@pytest.mark.postgres
@pytest.mark.parametrize("first_outcome", ["commit", "rollback"])
def test_postgres_second_booker_waits_for_first_transaction(engine, session_factory, first_outcome):
    """Deterministically interleave two booking transactions on PostgreSQL.

    Transaction A claims the slot and inserts its booking but does not commit yet.
    Booking B, started meanwhile, must block on A's row lock rather than read stale
    availability. When A commits, B fails with a conflict; when A rolls back, B wins.
    """
    with session_factory.begin() as setup:
        slot = Slot(clinic_id="clinic-1", start_at=_hours_from_now(24), end_at=_hours_from_now(25))
        setup.add(slot)

    session_a = session_factory()
    session_a.begin()
    claimed = session_a.execute(
        Slot.__table__.update()
        .where(Slot.id == slot.id, Slot.status == SlotStatus.AVAILABLE)
        .values(status=SlotStatus.BOOKED)
    ).rowcount
    assert claimed == 1
    session_a.add(Booking(slot_id=slot.id, patient_name="A", patient_email="a@example.com"))
    session_a.flush()
    session_a_pid = session_a.execute(text("SELECT pg_backend_pid()")).scalar_one()

    outcome: dict = {}
    finished = threading.Event()

    def attempt_b() -> None:
        with session_factory() as session_b:
            try:
                outcome["booking"] = book_slot(
                    session_b,
                    slot.id,
                    BookingCreate(patient_name="B", patient_email="b@example.com"),
                )
            except ConflictError as exc:
                outcome["error"] = exc.code
        finished.set()

    thread = threading.Thread(target=attempt_b)
    thread.start()
    _wait_until_blocked_by(engine, session_a_pid)
    assert not finished.is_set()

    if first_outcome == "commit":
        session_a.commit()
    else:
        session_a.rollback()
    session_a.close()
    thread.join(timeout=10)

    assert finished.is_set()
    if first_outcome == "commit":
        assert outcome == {"error": "slot_already_booked"}
    else:
        assert outcome["booking"].patient_name == "B"
    assert_booking_invariants(session_factory)


def _wait_until_blocked_by(engine, blocking_pid: int, timeout: float = 10) -> None:
    """Poll until PostgreSQL reports a backend waiting on a lock held by
    ``blocking_pid``. This proves the second transaction reached the conflicting row
    and is blocked, instead of assuming it from elapsed time."""
    deadline = time.monotonic() + timeout
    query = text("SELECT count(*) FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid))")
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            if connection.execute(query, {"pid": blocking_pid}).scalar_one() > 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"no transaction became blocked by backend {blocking_pid}")


def _hours_from_now(hours: int) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)
