from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from clinic_booking.db import Base

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def alembic_config(connection) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.attributes["connection"] = connection
    config.attributes["configure_logging"] = False
    return config


def test_migration_matches_models_and_enforces_invariant(engine):
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")

    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert diff == []
        indexes = {ix["name"] for ix in inspect(connection).get_indexes("bookings")}
        assert "uq_bookings_one_active_per_slot" in indexes

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO slots"
                " (id, clinic_id, start_at, end_at, status, created_at, updated_at)"
                " VALUES (1, 'c1', '2099-01-01 09:00:00+00', '2099-01-01 09:30:00+00',"
                " 'booked', '2099-01-01 00:00:00+00', '2099-01-01 00:00:00+00')"
            )
        )
    insert_booking = text(
        "INSERT INTO bookings (slot_id, patient_name, patient_email, status, created_at)"
        " VALUES (1, 'A', 'a@example.com', 'active', '2099-01-01 00:00:00+00')"
    )
    with engine.begin() as connection:
        connection.execute(insert_booking)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert_booking)

    with engine.begin() as connection:
        command.downgrade(alembic_config(connection), "base")
    with engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) <= {"alembic_version"}
