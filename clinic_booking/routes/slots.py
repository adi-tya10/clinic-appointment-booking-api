from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from clinic_booking.db import SessionDep
from clinic_booking.models import Booking, Slot
from clinic_booking.schemas import (
    MAX_ID,
    BookingCreate,
    BookingRead,
    SlotCreate,
    SlotFilters,
    SlotPage,
    SlotRead,
    error_responses,
)
from clinic_booking.services import bookings as booking_service
from clinic_booking.services import slots as slot_service

router = APIRouter(prefix="/slots", tags=["slots"])

SlotId = Annotated[int, Path(ge=1, le=MAX_ID)]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SlotRead,
    responses=error_responses(422),
    summary="Publish an appointment slot",
)
def create_slot(payload: SlotCreate, response: Response, session: SessionDep) -> Slot:
    slot = slot_service.create_slot(session, payload)
    response.headers["Location"] = f"/slots/{slot.id}"
    return slot


@router.get("", response_model=SlotPage, responses=error_responses(422), summary="List slots")
def list_slots(filters: Annotated[SlotFilters, Query()], session: SessionDep) -> SlotPage:
    slots = slot_service.list_slots(session, filters)
    return SlotPage(
        items=[SlotRead.model_validate(slot) for slot in slots],
        limit=filters.limit,
        offset=filters.offset,
    )


@router.get(
    "/{slot_id}", response_model=SlotRead, responses=error_responses(404, 422), summary="Get a slot"
)
def get_slot(slot_id: SlotId, session: SessionDep) -> Slot:
    return slot_service.get_slot(session, slot_id)


@router.post(
    "/{slot_id}/cancel",
    response_model=SlotRead,
    responses=error_responses(404, 409, 422),
    summary="Cancel an available slot",
    description="Only slots with status `available` can be cancelled. A booked slot returns "
    "409 `slot_has_active_booking`; cancel its booking first.",
)
def cancel_slot(slot_id: SlotId, session: SessionDep) -> Slot:
    return slot_service.cancel_slot(session, slot_id)


@router.post(
    "/{slot_id}/bookings",
    status_code=status.HTTP_201_CREATED,
    response_model=BookingRead,
    responses=error_responses(404, 409, 422),
    tags=["bookings"],
    summary="Book a slot for a patient",
    description="Succeeds only if the slot is `available` and has not started. Concurrent "
    "requests for the same slot are resolved by the database: exactly one gets 201, the "
    "others get 409 `slot_already_booked`.",
)
def book_slot(
    slot_id: SlotId, payload: BookingCreate, response: Response, session: SessionDep
) -> Booking:
    booking = booking_service.book_slot(session, slot_id, payload)
    response.headers["Location"] = f"/bookings/{booking.id}"
    return booking
