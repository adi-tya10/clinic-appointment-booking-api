import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from clinic_booking.services import slots as slot_service


@pytest.fixture
def unsafe_client(app):
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def assert_error_shape(response, status_code: int, code: str) -> dict:
    assert response.status_code == status_code
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    return body["error"]


def test_unknown_route_uses_error_shape(client):
    assert_error_shape(client.get("/nope"), 404, "not_found")


def test_wrong_method_uses_error_shape(client):
    error = assert_error_shape(client.delete("/slots"), 405, "method_not_allowed")
    assert error["message"] == "Method Not Allowed"


def test_validation_error_lists_each_problem(client):
    response = client.post("/slots", json={"clinic_id": "!", "start_at": "x"})
    error = assert_error_shape(response, 422, "validation_error")
    locations = {d["location"] for d in error["details"]}
    assert locations == {"body.clinic_id", "body.start_at", "body.end_at"}
    assert all(set(d) == {"location", "message"} for d in error["details"])


def test_unexpected_error_returns_generic_500(unsafe_client, monkeypatch):
    def explode(*_args):
        raise RuntimeError("password=hunter2 at /srv/app/db.py line 12")

    monkeypatch.setattr(slot_service, "list_slots", explode)

    response = unsafe_client.get("/slots")

    error = assert_error_shape(response, 500, "internal_error")
    assert error["message"] == "An unexpected error occurred."
    assert "hunter2" not in response.text and "Traceback" not in response.text


def test_database_outage_returns_503_without_details(unsafe_client, monkeypatch):
    def unavailable(*_args):
        raise OperationalError("SELECT secret_table", {}, Exception("connection refused"))

    monkeypatch.setattr(slot_service, "get_slot", unavailable)

    response = unsafe_client.get("/slots/1")

    assert_error_shape(response, 503, "database_unavailable")
    assert "secret_table" not in response.text


def test_health_reports_database_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_documents_endpoints_and_error_model(client):
    spec = client.get("/openapi.json").json()

    assert set(spec["paths"]) == {
        "/slots",
        "/slots/{slot_id}",
        "/slots/{slot_id}/cancel",
        "/slots/{slot_id}/bookings",
        "/bookings",
        "/bookings/{booking_id}",
        "/bookings/{booking_id}/cancel",
        "/health",
    }
    booking_responses = spec["paths"]["/slots/{slot_id}/bookings"]["post"]["responses"]
    assert {"201", "404", "409", "422"} <= set(booking_responses)
    assert booking_responses["409"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorResponse"
    )
