from datetime import UTC, datetime, timedelta

import httpx2
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from clinic_booking.models import Booking, BookingStatus, Slot, SlotStatus


def iso_in(hours: float = 0, days: float = 0) -> str:
    return (datetime.now(UTC) + timedelta(days=days, hours=hours)).isoformat()


def slot_payload(**overrides) -> dict:
    payload = {
        "clinic_id": "clinic-1",
        "start_at": iso_in(days=1),
        "end_at": iso_in(days=1, hours=0.5),
    }
    payload.update(overrides)
    return payload


def booking_payload(n: int = 1) -> dict:
    return {"patient_name": f"Patient {n}", "patient_email": f"patient{n}@example.com"}


def create_slot(client: TestClient | httpx2.Client, **overrides) -> dict:
    response = client.post("/slots", json=slot_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def book(client: TestClient | httpx2.Client, slot_id: int, n: int = 1) -> dict:
    response = client.post(f"/slots/{slot_id}/bookings", json=booking_payload(n))
    assert response.status_code == 201, response.text
    return response.json()


def assert_booking_invariants(session_factory: sessionmaker[Session]) -> None:
    """Every slot has at most one active booking, and a slot is 'booked' exactly
    when it has one."""
    with session_factory() as session:
        active_counts = dict(
            session.execute(
                select(Booking.slot_id, func.count())
                .where(Booking.status == BookingStatus.ACTIVE)
                .group_by(Booking.slot_id)
            ).all()
        )
        for slot in session.scalars(select(Slot)):
            active = active_counts.get(slot.id, 0)
            assert active <= 1, f"slot {slot.id} has {active} active bookings"
            assert (slot.status == SlotStatus.BOOKED) == (active == 1), (
                f"slot {slot.id} is {slot.status} with {active} active bookings"
            )
