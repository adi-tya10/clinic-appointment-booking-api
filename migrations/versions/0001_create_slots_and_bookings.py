"""create slots and bookings

Revision ID: 0001
Revises:
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "slots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("clinic_id", sa.String(64), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("end_at > start_at", name="ck_slots_end_after_start"),
        sa.CheckConstraint(
            "status IN ('available', 'booked', 'cancelled')", name="ck_slots_status_valid"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_slots"),
    )
    op.create_index("ix_slots_clinic_id_start_at", "slots", ["clinic_id", "start_at"])

    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slot_id", sa.Integer(), nullable=False),
        sa.Column("patient_name", sa.String(200), nullable=False),
        sa.Column("patient_email", sa.String(254), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active', 'cancelled')", name="ck_bookings_status_valid"),
        sa.CheckConstraint(
            "(status = 'active' AND cancelled_at IS NULL)"
            " OR (status = 'cancelled' AND cancelled_at IS NOT NULL)",
            name="ck_bookings_cancelled_at_matches_status",
        ),
        sa.ForeignKeyConstraint(
            ["slot_id"], ["slots.id"], name="fk_bookings_slot_id_slots", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_bookings"),
    )
    op.create_index("ix_bookings_slot_id", "bookings", ["slot_id"])
    op.create_index(
        "uq_bookings_one_active_per_slot",
        "bookings",
        ["slot_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_bookings_one_active_per_slot", table_name="bookings")
    op.drop_index("ix_bookings_slot_id", table_name="bookings")
    op.drop_table("bookings")
    op.drop_index("ix_slots_clinic_id_start_at", table_name="slots")
    op.drop_table("slots")
