import logging
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Row, select, update
from sqlalchemy.orm import Session

from clinic_booking.db import utcnow
from clinic_booking.errors import ConflictError, NotFoundError
from clinic_booking.models import Slot, SlotStatus
from clinic_booking.schemas import SlotCreate, SlotFilters

logger = logging.getLogger(__name__)

# A conditional update can match nothing because the slot was in another state at that
# instant and changed back before the reason was looked up (e.g. a booking cancelled
# meanwhile). The operation is then simply tried again; the bound only matters if the
# slot keeps flipping under heavy contention.
MAX_CLAIM_ATTEMPTS = 3


def slot_not_found(slot_id: int) -> NotFoundError:
    return NotFoundError("slot_not_found", f"Slot {slot_id} does not exist.")


def read_slot_state(session: Session, slot_id: int) -> Row[tuple[SlotStatus, datetime]] | None:
    """Read a slot's current status outside any write transaction.

    Plain column values are returned rather than an ORM instance, and the read
    transaction is ended so the caller can start a fresh one for a retry.
    """
    state = session.execute(
        select(Slot.status, Slot.start_at).where(Slot.id == slot_id)
    ).one_or_none()
    session.rollback()
    return state


def create_slot(session: Session, data: SlotCreate) -> Slot:
    slot = Slot(clinic_id=data.clinic_id, start_at=data.start_at, end_at=data.end_at)
    with session.begin():
        session.add(slot)
    logger.info("Created slot %s for clinic %s", slot.id, slot.clinic_id)
    return slot


def get_slot(session: Session, slot_id: int) -> Slot:
    slot = session.get(Slot, slot_id)
    if slot is None:
        raise slot_not_found(slot_id)
    return slot


def list_slots(session: Session, filters: SlotFilters) -> Sequence[Slot]:
    query = select(Slot)
    if filters.clinic_id is not None:
        query = query.where(Slot.clinic_id == filters.clinic_id)
    if filters.status is not None:
        query = query.where(Slot.status == filters.status)
    if filters.starts_from is not None:
        query = query.where(Slot.start_at >= filters.starts_from)
    if filters.starts_before is not None:
        query = query.where(Slot.start_at < filters.starts_before)
    query = query.order_by(Slot.start_at, Slot.id).limit(filters.limit).offset(filters.offset)
    return session.scalars(query).all()


def cancel_slot(session: Session, slot_id: int) -> Slot:
    """Withdraw an available slot. A slot with an active booking cannot be cancelled;
    the booking must be cancelled first so no patient is left with an orphaned booking.
    """
    for _ in range(MAX_CLAIM_ATTEMPTS):
        now = utcnow()
        with session.begin():
            # The status check and the write are one statement, so a booking racing
            # this cancellation either commits first (and we match nothing) or waits.
            cancelled = session.execute(
                update(Slot)
                .where(Slot.id == slot_id, Slot.status == SlotStatus.AVAILABLE)
                .values(status=SlotStatus.CANCELLED, updated_at=now)
                .returning(Slot)
            ).scalar_one_or_none()
        if cancelled is not None:
            logger.info("Cancelled slot %s", slot_id)
            return cancelled

        state = read_slot_state(session, slot_id)
        if state is None:
            raise slot_not_found(slot_id)
        if state.status == SlotStatus.CANCELLED:
            raise ConflictError("slot_already_cancelled", f"Slot {slot_id} is already cancelled.")
        if state.status == SlotStatus.BOOKED:
            raise ConflictError(
                "slot_has_active_booking",
                f"Slot {slot_id} has an active booking. "
                "Cancel the booking before cancelling the slot.",
            )
    raise ConflictError("slot_state_changed", f"Slot {slot_id} changed concurrently. Please retry.")
