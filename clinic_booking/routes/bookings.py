from typing import Annotated

from fastapi import APIRouter, Path, Query

from clinic_booking.db import SessionDep
from clinic_booking.models import Booking
from clinic_booking.schemas import (
    MAX_ID,
    BookingFilters,
    BookingPage,
    BookingRead,
    error_responses,
)
from clinic_booking.services import bookings as booking_service

router = APIRouter(prefix="/bookings", tags=["bookings"])

BookingId = Annotated[int, Path(ge=1, le=MAX_ID)]


@router.get("", response_model=BookingPage, responses=error_responses(422), summary="List bookings")
def list_bookings(filters: Annotated[BookingFilters, Query()], session: SessionDep) -> BookingPage:
    bookings = booking_service.list_bookings(session, filters)
    return BookingPage(
        items=[BookingRead.model_validate(booking) for booking in bookings],
        limit=filters.limit,
        offset=filters.offset,
    )


@router.get(
    "/{booking_id}",
    response_model=BookingRead,
    responses=error_responses(404, 422),
    summary="Get a booking",
)
def get_booking(booking_id: BookingId, session: SessionDep) -> Booking:
    return booking_service.get_booking(session, booking_id)


@router.post(
    "/{booking_id}/cancel",
    response_model=BookingRead,
    responses=error_responses(404, 409, 422),
    summary="Cancel a booking",
    description="Marks an active booking as cancelled (the record is kept) and makes its slot "
    "available again in the same transaction. Cancelling twice returns 409.",
)
def cancel_booking(booking_id: BookingId, session: SessionDep) -> Booking:
    return booking_service.cancel_booking(session, booking_id)
