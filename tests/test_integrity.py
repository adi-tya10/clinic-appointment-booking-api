"""Database-level guarantees and rollback behaviour when a transaction fails midway."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError

from clinic_booking.models import Booking, BookingStatus, Slot, SlotStatus
from tests.helpers import assert_booking_invariants, book, booking_payload, create_slot


def _future(hours: int) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


FIXED_START = datetime(2099, 1, 1, 9, 0, tzinfo=UTC)


@contextmanager
def failing_statement(engine, prefix: str) -> Iterator[None]:
    """Make the next SQL statement starting with ``prefix`` fail, mid-transaction."""

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(prefix):
            raise RuntimeError("simulated failure")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", fail)


@pytest.fixture
def unsafe_client(app) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_database_rejects_second_active_booking_for_a_slot(session_factory):
    with session_factory.begin() as session:
        slot = Slot(
            clinic_id="c1", start_at=_future(1), end_at=_future(2), status=SlotStatus.BOOKED
        )
        session.add(slot)
        session.flush()
        session.add(Booking(slot_id=slot.id, patient_name="A", patient_email="a@example.com"))

    with pytest.raises(IntegrityError), session_factory.begin() as session:
        session.add(Booking(slot_id=slot.id, patient_name="B", patient_email="b@example.com"))


def test_database_allows_many_cancelled_bookings_for_a_slot(session_factory):
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        slot = Slot(clinic_id="c1", start_at=_future(1), end_at=_future(2))
        session.add(slot)
        session.flush()
        for n in range(3):
            session.add(
                Booking(
                    slot_id=slot.id,
                    patient_name=f"P{n}",
                    patient_email=f"p{n}@example.com",
                    status=BookingStatus.CANCELLED,
                    cancelled_at=now,
                )
            )
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Booking)) == 3


@pytest.mark.parametrize(
    "build",
    [
        lambda: Slot(
            clinic_id="c1", start_at=FIXED_START, end_at=FIXED_START - timedelta(minutes=1)
        ),
        lambda: Slot(clinic_id="c1", start_at=FIXED_START, end_at=FIXED_START),
    ],
)
def test_database_rejects_slot_ending_before_it_starts(session_factory, build):
    with pytest.raises(IntegrityError), session_factory.begin() as session:
        session.add(build())


def test_database_rejects_cancelled_booking_without_timestamp(session_factory):
    with session_factory.begin() as session:
        slot = Slot(clinic_id="c1", start_at=_future(1), end_at=_future(2))
        session.add(slot)
    with pytest.raises(IntegrityError), session_factory.begin() as session:
        session.add(
            Booking(
                slot_id=slot.id,
                patient_name="A",
                patient_email="a@example.com",
                status=BookingStatus.CANCELLED,
            )
        )


def test_database_rejects_booking_for_missing_slot(session_factory):
    with pytest.raises(IntegrityError), session_factory.begin() as session:
        session.add(Booking(slot_id=999, patient_name="A", patient_email="a@example.com"))


def test_constraint_violation_rolls_back_slot_claim(client, session_factory):
    """If the booking insert hits the unique index, the slot update is undone too."""
    slot = create_slot(client)
    # Corrupt the data on purpose: an active booking exists but the slot says available.
    with session_factory.begin() as session:
        session.add(Booking(slot_id=slot["id"], patient_name="X", patient_email="x@example.com"))

    response = client.post(f"/slots/{slot['id']}/bookings", json=booking_payload())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "slot_already_booked"
    assert "UNIQUE" not in response.text and "IntegrityError" not in response.text
    with session_factory() as session:
        assert session.get(Slot, slot["id"]).status == SlotStatus.AVAILABLE
        assert session.scalar(select(func.count()).select_from(Booking)) == 1


def test_failed_booking_insert_leaves_slot_available(engine, unsafe_client, session_factory):
    slot = create_slot(unsafe_client)

    with failing_statement(engine, "INSERT INTO BOOKINGS"):
        response = unsafe_client.post(f"/slots/{slot['id']}/bookings", json=booking_payload())

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An unexpected error occurred."}
    }
    assert unsafe_client.get(f"/slots/{slot['id']}").json()["status"] == "available"
    assert unsafe_client.get("/bookings").json()["items"] == []
    book(unsafe_client, slot["id"])
    assert_booking_invariants(session_factory)


def test_failed_slot_release_keeps_booking_active(engine, unsafe_client, session_factory):
    slot = create_slot(unsafe_client)
    booking = book(unsafe_client, slot["id"])

    with failing_statement(engine, "UPDATE SLOTS"):
        response = unsafe_client.post(f"/bookings/{booking['id']}/cancel")

    assert response.status_code == 500
    after_failure = unsafe_client.get(f"/bookings/{booking['id']}").json()
    assert (after_failure["status"], after_failure["cancelled_at"]) == ("active", None)
    assert unsafe_client.get(f"/slots/{slot['id']}").json()["status"] == "booked"
    assert_booking_invariants(session_factory)

    retry = unsafe_client.post(f"/bookings/{booking['id']}/cancel")
    assert retry.status_code == 200
    assert unsafe_client.get(f"/slots/{slot['id']}").json()["status"] == "available"


@pytest.mark.parametrize("corrupt_slot_status", [SlotStatus.AVAILABLE, SlotStatus.CANCELLED])
def test_cancelling_booking_whose_slot_is_not_booked_restores_consistency(
    client, session_factory, corrupt_slot_status, caplog
):
    """The slot release updates zero rows only if data was corrupted outside the API
    (active booking, slot not 'booked'). Committing the cancellation must leave the
    invariant intact rather than roll back and keep the booking stuck as active."""
    slot = create_slot(client)
    booking = book(client, slot["id"])
    with session_factory.begin() as session:
        session.get(Slot, slot["id"]).status = corrupt_slot_status

    response = client.post(f"/bookings/{booking['id']}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert client.get(f"/slots/{slot['id']}").json()["status"] == corrupt_slot_status
    assert "was not booked while booking" in caplog.text
    assert_booking_invariants(session_factory)
