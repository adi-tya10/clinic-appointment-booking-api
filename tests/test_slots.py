from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers import book, create_slot, iso_in, slot_payload


def test_create_slot_returns_created_slot_in_utc(client):
    response = client.post(
        "/slots",
        json={
            "clinic_id": "clinic-42",
            "start_at": "2099-03-01T09:00:00+05:30",
            "end_at": "2099-03-01T09:30:00+05:30",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert response.headers["location"] == f"/slots/{body['id']}"
    assert body["clinic_id"] == "clinic-42"
    assert body["status"] == "available"
    assert datetime.fromisoformat(body["start_at"]) == datetime(2099, 3, 1, 3, 30, tzinfo=UTC)
    assert body["start_at"].endswith("Z")
    assert client.get(f"/slots/{body['id']}").json() == body


def test_list_slots_orders_by_start_time(client):
    later = create_slot(client, start_at=iso_in(days=3), end_at=iso_in(days=3, hours=1))
    sooner = create_slot(client, start_at=iso_in(days=2), end_at=iso_in(days=2, hours=1))

    response = client.get("/slots")

    assert response.status_code == 200
    assert [s["id"] for s in response.json()["items"]] == [sooner["id"], later["id"]]
    assert response.json()["limit"] == 50
    assert response.json()["offset"] == 0


def test_list_slots_filters_by_clinic_status_and_time(client):
    tomorrow = create_slot(client, clinic_id="north")
    booked = create_slot(client, clinic_id="north")
    book(client, booked["id"])
    next_week = create_slot(
        client, clinic_id="north", start_at=iso_in(days=7), end_at=iso_in(days=7, hours=1)
    )
    create_slot(client, clinic_id="south")

    def ids(**params):
        response = client.get("/slots", params=params)
        assert response.status_code == 200
        return {s["id"] for s in response.json()["items"]}

    assert ids(clinic_id="north") == {tomorrow["id"], booked["id"], next_week["id"]}
    assert ids(clinic_id="north", status="available") == {tomorrow["id"], next_week["id"]}
    assert ids(clinic_id="north", status="booked") == {booked["id"]}
    assert ids(clinic_id="north", starts_from=iso_in(days=5)) == {next_week["id"]}
    assert ids(clinic_id="north", starts_before=iso_in(days=5), status="available") == {
        tomorrow["id"]
    }


def test_list_slots_paginates(client):
    created = [
        create_slot(client, start_at=iso_in(days=d), end_at=iso_in(days=d, hours=1))["id"]
        for d in range(1, 6)
    ]

    page = client.get("/slots", params={"limit": 2, "offset": 2}).json()

    assert [s["id"] for s in page["items"]] == created[2:4]
    assert (page["limit"], page["offset"]) == (2, 2)


def test_overlapping_slots_for_same_clinic_are_allowed(client):
    first = create_slot(client)
    second = create_slot(client, start_at=first["start_at"], end_at=first["end_at"])
    assert first["id"] != second["id"]


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        ({"end_at": iso_in(days=1)}, "end_at must be after start_at"),
        (
            {"start_at": iso_in(days=1, hours=1), "end_at": iso_in(days=1)},
            "end_at must be after start_at",
        ),
        (
            {"start_at": iso_in(hours=-2), "end_at": iso_in(hours=-1)},
            "start_at must be in the future",
        ),
        ({"end_at": iso_in(days=2, hours=1)}, "longer than 24 hours"),
    ],
)
def test_create_slot_rejects_invalid_time_ranges(client, overrides, expected_message):
    response = client.post("/slots", json=slot_payload(**overrides))

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert any(expected_message in d["message"] for d in error["details"])
    assert client.get("/slots").json()["items"] == []


@pytest.mark.parametrize(
    ("payload", "location"),
    [
        (slot_payload(start_at="2099-03-01T09:00:00"), "body.start_at"),
        (slot_payload(end_at="tomorrow"), "body.end_at"),
        (slot_payload(clinic_id=""), "body.clinic_id"),
        (slot_payload(clinic_id="clinic 1; DROP TABLE slots"), "body.clinic_id"),
        (slot_payload(clinic_id="x" * 65), "body.clinic_id"),
        (slot_payload(clinic_id=42), "body.clinic_id"),
        ({"start_at": iso_in(days=1), "end_at": iso_in(days=1, hours=1)}, "body.clinic_id"),
        (slot_payload(status="booked"), "body.status"),
    ],
)
def test_create_slot_rejects_malformed_fields(client, payload, location):
    response = client.post("/slots", json=payload)

    assert response.status_code == 422
    assert location in [d["location"] for d in response.json()["error"]["details"]]


def test_create_slot_rejects_non_object_and_broken_json(client):
    assert client.post("/slots", json=["not", "an", "object"]).status_code == 422
    response = client.post(
        "/slots", content=b'{"clinic_id": ', headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "params",
    [
        {"status": "unknown"},
        {"starts_from": "2099-01-01T00:00:00"},
        {"starts_from": iso_in(days=2), "starts_before": iso_in(days=1)},
        {"limit": 0},
        {"limit": 201},
        {"offset": -1},
    ],
)
def test_list_slots_rejects_invalid_filters(client, params):
    response = client.get("/slots", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_cancel_available_slot(client):
    slot = create_slot(client)

    response = client.post(f"/slots/{slot['id']}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert client.get(f"/slots/{slot['id']}").json()["status"] == "cancelled"
    assert client.get("/slots", params={"status": "available"}).json()["items"] == []


def test_cancelling_slot_twice_conflicts(client):
    slot = create_slot(client)
    client.post(f"/slots/{slot['id']}/cancel")

    response = client.post(f"/slots/{slot['id']}/cancel")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "slot_already_cancelled"


def test_cancelling_slot_with_active_booking_is_rejected_and_booking_kept(client):
    slot = create_slot(client)
    booking = book(client, slot["id"])

    response = client.post(f"/slots/{slot['id']}/cancel")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "slot_has_active_booking"
    assert client.get(f"/slots/{slot['id']}").json()["status"] == "booked"
    assert client.get(f"/bookings/{booking['id']}").json()["status"] == "active"


def test_slot_can_be_cancelled_after_its_booking_is_cancelled(client):
    slot = create_slot(client)
    booking = book(client, slot["id"])
    client.post(f"/bookings/{booking['id']}/cancel")

    response = client.post(f"/slots/{slot['id']}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


@pytest.mark.parametrize("method_path", [("GET", "/slots/999"), ("POST", "/slots/999/cancel")])
def test_missing_slot_returns_404(client, method_path):
    method, path = method_path
    response = client.request(method, path)
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "slot_not_found", "message": "Slot 999 does not exist."}
    }


@pytest.mark.parametrize("slot_id", ["abc", "0", "-1", "1.5", str(2**31), str(10**30)])
def test_invalid_slot_id_returns_422(client, slot_id):
    response = client.get(f"/slots/{slot_id}")
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["location"] == "path.slot_id"


def test_slot_timestamps_survive_round_trip_with_microseconds(client):
    start = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=123456)
    slot = create_slot(
        client, start_at=start.isoformat(), end_at=(start + timedelta(minutes=15)).isoformat()
    )
    assert datetime.fromisoformat(slot["start_at"]) == start
