from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from clinic_booking.db import Base, UTCDateTime, utcnow


class SlotStatus(StrEnum):
    AVAILABLE = "available"
    BOOKED = "booked"
    CANCELLED = "cancelled"


class BookingStatus(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"


def _enum_column(enum_cls: type[StrEnum]) -> Enum:
    # Stored as plain strings; the allowed values are enforced by explicit CHECK
    # constraints on each table so the migration reads the same as the model.
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=16,
        values_callable=lambda members: [member.value for member in members],
    )


class Slot(Base):
    __tablename__ = "slots"
    __table_args__ = (
        CheckConstraint("end_at > start_at", name="end_after_start"),
        CheckConstraint("status IN ('available', 'booked', 'cancelled')", name="status_valid"),
        Index("ix_slots_clinic_id_start_at", "clinic_id", "start_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clinic_id: Mapped[str] = mapped_column(String(64))
    start_at: Mapped[datetime] = mapped_column(UTCDateTime())
    end_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[SlotStatus] = mapped_column(
        _enum_column(SlotStatus), default=SlotStatus.AVAILABLE
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'cancelled')", name="status_valid"),
        CheckConstraint(
            "(status = 'active' AND cancelled_at IS NULL)"
            " OR (status = 'cancelled' AND cancelled_at IS NOT NULL)",
            name="cancelled_at_matches_status",
        ),
        # The database-level guarantee against double-booking: whatever the
        # application does, a slot can have at most one active booking.
        Index(
            "uq_bookings_one_active_per_slot",
            "slot_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id", ondelete="RESTRICT"), index=True)
    patient_name: Mapped[str] = mapped_column(String(200))
    patient_email: Mapped[str] = mapped_column(String(254))
    status: Mapped[BookingStatus] = mapped_column(
        _enum_column(BookingStatus), default=BookingStatus.ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
