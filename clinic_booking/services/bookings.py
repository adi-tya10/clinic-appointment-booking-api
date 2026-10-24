import logging
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clinic_booking.db import utcnow
from clinic_booking.errors import ApiError, ConflictError, NotFoundError
from clinic_booking.models import Booking, BookingStatus, Slot, SlotStatus
from clinic_booking.schemas import BookingCreate, BookingFilters
from clinic_booking.services.slots import MAX_CLAIM_ATTEMPTS, read_slot_state, slot_not_found

logger = logging.getLogger(__name__)


def booking_not_found(booking_id: int) -> NotFoundError:
    return NotFoundError("booking_not_found", f"Booking {booking_id} does not exist.")


def slot_already_booked(slot_id: int) -> ConflictError:
    return ConflictError("slot_already_booked", f"Slot {slot_id} is already booked.")


def book_slot(session: Session, slot_id: int, data: BookingCreate) -> Booking:
    """Book a slot atomically.

    The slot is claimed with a conditional UPDATE (available -> booked) and the
    booking row is inserted in the same transaction. The database lets only one
    concurrent transaction change the row from 'available', so at most one request
    can claim it; the others update zero rows and get a 409. The partial unique index
    on bookings is a second, independent guard: if it is ever violated the whole
    transaction rolls back, including the slot status change.
    """
    for _ in range(MAX_CLAIM_ATTEMPTS):
        now = utcnow()
        booking = _try_book(session, slot_id, data, now)
        if booking is not None:
            logger.info("Created booking %s for slot %s", booking.id, slot_id)
            return booking
        rejection = _explain_unbookable(session, slot_id, now)
        if rejection is not None:
            raise rejection
    raise ConflictError("slot_state_changed", f"Slot {slot_id} changed concurrently. Please retry.")


def _try_book(session: Session, slot_id: int, data: BookingCreate, now: datetime) -> Booking | None:
    try:
        with session.begin():
            claimed = session.execute(
                update(Slot)
                .where(
                    Slot.id == slot_id,
                    Slot.status == SlotStatus.AVAILABLE,
                    Slot.start_at > now,
                )
                .values(status=SlotStatus.BOOKED, updated_at=now)
            ).rowcount
            if claimed != 1:
                return None
            booking = Booking(
                slot_id=slot_id, patient_name=data.patient_name, patient_email=data.patient_email
            )
            session.add(booking)
    except IntegrityError as exc:
        logger.warning("Booking for slot %s rejected by database constraint", slot_id)
        raise slot_already_booked(slot_id) from exc
    return booking


def _explain_unbookable(session: Session, slot_id: int, now: datetime) -> ApiError | None:
    """Return the reason a claim failed, or None if the slot looks bookable again."""
    state = read_slot_state(session, slot_id)
    if state is None:
        return slot_not_found(slot_id)
    if state.status == SlotStatus.BOOKED:
        return slot_already_booked(slot_id)
    if state.status == SlotStatus.CANCELLED:
        return ConflictError("slot_cancelled", f"Slot {slot_id} has been cancelled.")
    if state.start_at <= now:
        return ConflictError("slot_already_started", f"Slot {slot_id} has already started.")
    return None


def get_booking(session: Session, booking_id: int) -> Booking:
    booking = session.get(Booking, booking_id)
    if booking is None:
        raise booking_not_found(booking_id)
    return booking


def list_bookings(session: Session, filters: BookingFilters) -> Sequence[Booking]:
    query = select(Booking)
    if filters.slot_id is not None:
        query = query.where(Booking.slot_id == filters.slot_id)
    if filters.status is not None:
        query = query.where(Booking.status == filters.status)
    query = query.order_by(Booking.id).limit(filters.limit).offset(filters.offset)
    return session.scalars(query).all()


def cancel_booking(session: Session, booking_id: int) -> Booking:
    """Cancel an active booking and release its slot in the same transaction.

    The booking row is kept with status 'cancelled' as a historical record.
    """
    now = utcnow()
    with session.begin():
        booking = session.execute(
            update(Booking)
            .where(Booking.id == booking_id, Booking.status == BookingStatus.ACTIVE)
            .values(status=BookingStatus.CANCELLED, cancelled_at=now)
            .returning(Booking)
        ).scalar_one_or_none()
        if booking is not None:
            released = session.execute(
                update(Slot)
                .where(Slot.id == booking.slot_id, Slot.status == SlotStatus.BOOKED)
                .values(status=SlotStatus.AVAILABLE, updated_at=now)
            ).rowcount
            if released != 1:
                # Unreachable through the API: an active booking always holds a booked
                # slot. If data was corrupted outside the API, committing is still right:
                # the slot is not booked and no longer has an active booking, which is the
                # consistent state. Rolling back would keep the corruption and make the
                # booking impossible to cancel.
                logger.error(
                    "Slot %s was not booked while booking %s was active",
                    booking.slot_id,
                    booking_id,
                )

    if booking is None:
        existing = get_booking(session, booking_id)
        if existing.status == BookingStatus.CANCELLED:
            raise ConflictError(
                "booking_already_cancelled", f"Booking {booking_id} is already cancelled."
            )
        raise ConflictError(
            "booking_state_changed", f"Booking {booking_id} changed concurrently. Please retry."
        )
    logger.info("Cancelled booking %s and released slot %s", booking_id, booking.slot_id)
    return booking
