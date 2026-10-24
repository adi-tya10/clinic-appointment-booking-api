from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from clinic_booking.models import Booking, Slot, SlotStatus
from tests.helpers import assert_booking_invariants, book, booking_payload, create_slot


def test_book_available_slot(client, session_factory):
    slot = create_slot(client)

    response = client.post(
        f"/slots/{slot['id']}/bookings",
        json={"patient_name": "  Asha Rao ", "patient_email": "asha.rao@example.com"},
    )

    assert response.status_code == 201
    booking = response.json()
    assert response.headers["location"] == f"/bookings/{booking['id']}"
    assert booking["slot_id"] == slot["id"]
    assert booking["patient_name"] == "Asha Rao"
    assert booking["status"] == "active"
    assert booking["cancelled_at"] is None
    assert client.get(f"/slots/{slot['id']}").json()["status"] == "booked"
    assert client.get(f"/bookings/{booking['id']}").json() == booking
    assert_booking_invariants(session_factory)


def test_book_already_booked_slot_conflicts(client, session_factory):
    slot = create_slot(client)
    first = book(client, slot["id"], n=1)

    response = client.post(f"/slots/{slot['id']}/bookings", json=booking_payload(2))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "slot_already_booked"
    bookings = client.get("/bookings", params={"slot_id": slot["id"]}).json()["items"]
    assert [b["id"] for b in bookings] == [first["id"]]
    assert_booking_invariants(session_factory)


def test_duplicate_booking_request_from_same_patient_conflicts(client):
    slot = create_slot(client)
    book(client, slot["id"], n=1)

    response = client.post(f"/slots/{slot['id']}/bookings", json=booking_payload(1))

    assert response.status_code == 409
    assert len(client.get("/bookings").json()["items"]) == 1


def test_booking_other_slot_succeeds_while_one_is_booked(client, session_factory):
    first = create_slot(client)
    second = create_slot(client)
    book(client, first["id"], n=1)

    booking = book(client, second["id"], n=2)

    assert booking["slot_id"] == second["id"]
    statuses = {s["id"]: s["status"] for s in client.get("/slots").json()["items"]}
    assert statuses == {first["id"]: "booked", second["id"]: "booked"}
    assert_booking_invariants(session_factory)


def test_cancel_booking_keeps_record_and_frees_slot(client, session_factory):
    slot = create_slot(client)
    booking = book(client, slot["id"])

    response = client.post(f"/bookings/{booking['id']}/cancel")

    assert response.status_code == 200
    cancelled = response.json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancelled_at"] is not None
    assert cancelled["patient_email"] == booking["patient_email"]
    assert client.get(f"/bookings/{booking['id']}").json()["status"] == "cancelled"
    assert client.get(f"/slots/{slot['id']}").json()["status"] == "available"
    assert_booking_invariants(session_factory)


def test_rebook_after_cancellation(client, session_factory):
    slot = create_slot(client)
    original = book(client, slot["id"], n=1)
    client.post(f"/bookings/{original['id']}/cancel")

    rebooked = book(client, slot["id"], n=2)

    assert rebooked["id"] != original["id"]
    history = client.get("/bookings", params={"slot_id": slot["id"]}).json()["items"]
    assert [(b["id"], b["status"]) for b in history] == [
        (original["id"], "cancelled"),
        (rebooked["id"], "active"),
    ]
    assert client.get("/bookings", params={"status": "active"}).json()["items"] == [rebooked]
    assert_booking_invariants(session_factory)


def test_cancelling_booking_twice_conflicts_without_side_effects(client, session_factory):
    slot = create_slot(client)
    first = book(client, slot["id"], n=1)
    client.post(f"/bookings/{first['id']}/cancel")
    second = book(client, slot["id"], n=2)

    response = client.post(f"/bookings/{first['id']}/cancel")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "booking_already_cancelled"
    # The repeated cancel must not release the slot now held by the second booking.
    assert client.get(f"/slots/{slot['id']}").json()["status"] == "booked"
    assert client.get(f"/bookings/{second['id']}").json()["status"] == "active"
    assert_booking_invariants(session_factory)


def test_booking_cancelled_slot_conflicts(client):
    slot = create_slot(client)
    client.post(f"/slots/{slot['id']}/cancel")

    response = client.post(f"/slots/{slot['id']}/bookings", json=booking_payload())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "slot_cancelled"
    assert client.get("/bookings").json()["items"] == []


def test_booking_slot_that_already_started_conflicts(client, session_factory):
    started = datetime.now(UTC) - timedelta(minutes=5)
    with session_factory.begin() as session:
        slot = Slot(clinic_id="clinic-1", start_at=started, end_at=started + timedelta(minutes=30))
        session.add(slot)
    response = client.post(f"/slots/{slot.id}/bookings", json=booking_payload())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "slot_already_started"
    assert client.get(f"/slots/{slot.id}").json()["status"] == "available"


def test_booking_missing_slot_returns_404(client):
    response = client.post("/slots/12345/bookings", json=booking_payload())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "slot_not_found"


@pytest.mark.parametrize(
    "method_path", [("GET", "/bookings/777"), ("POST", "/bookings/777/cancel")]
)
def test_missing_booking_returns_404(client, method_path):
    response = client.request(*method_path)
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "booking_not_found", "message": "Booking 777 does not exist."}
    }


@pytest.mark.parametrize(
    ("payload", "location"),
    [
        ({"patient_name": "Asha"}, "body.patient_email"),
        ({"patient_email": "asha@example.com"}, "body.patient_name"),
        ({"patient_name": "   ", "patient_email": "asha@example.com"}, "body.patient_name"),
        ({"patient_name": "A" * 201, "patient_email": "asha@example.com"}, "body.patient_name"),
        ({"patient_name": "Asha", "patient_email": "not-an-email"}, "body.patient_email"),
        ({"patient_name": 7, "patient_email": "asha@example.com"}, "body.patient_name"),
        ({**booking_payload(), "status": "cancelled"}, "body.status"),
    ],
)
def test_book_rejects_invalid_payload_without_touching_slot(
    client, session_factory, payload, location
):
    slot = create_slot(client)

    response = client.post(f"/slots/{slot['id']}/bookings", json=payload)

    assert response.status_code == 422
    assert location in [d["location"] for d in response.json()["error"]["details"]]
    with session_factory() as session:
        assert session.get(Slot, slot["id"]).status == SlotStatus.AVAILABLE
        assert session.scalar(select(func.count()).select_from(Booking)) == 0


def test_validation_errors_do_not_echo_patient_data(client):
    slot = create_slot(client)
    response = client.post(
        f"/slots/{slot['id']}/bookings",
        json={"patient_name": "Secret Person", "patient_email": "secret-but-invalid"},
    )
    assert response.status_code == 422
    assert "secret" not in response.text.lower()


@pytest.mark.parametrize("params", [{"slot_id": "x"}, {"slot_id": 0}, {"status": "booked"}])
def test_list_bookings_rejects_invalid_filters(client, params):
    assert client.get("/bookings", params=params).status_code == 422


def test_list_bookings_paginates(client):
    ids = [book(client, create_slot(client)["id"], n=n)["id"] for n in range(5)]

    page = client.get("/bookings", params={"limit": 2, "offset": 2}).json()

    assert [b["id"] for b in page["items"]] == ids[2:4]
    assert (page["limit"], page["offset"]) == (2, 2)


@pytest.mark.parametrize("booking_id", ["abc", "0", "-1", str(2**31)])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_invalid_booking_id_returns_422(client, method, booking_id):
    path = f"/bookings/{booking_id}" + ("/cancel" if method == "POST" else "")
    response = client.request(method, path)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["location"] == "path.booking_id"
