import logging

from fastapi import FastAPI
from sqlalchemy import Engine

from clinic_booking.config import get_settings
from clinic_booking.db import create_db_engine, create_session_factory
from clinic_booking.errors import register_error_handlers
from clinic_booking.routes import bookings, health, slots


def create_app(engine: Engine | None = None) -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if engine is None:
        engine = create_db_engine(settings.database_url)

    app = FastAPI(
        title="Clinic Appointment Booking API",
        version="0.1.0",
        description="Clinics publish appointment slots; patients book and cancel them. "
        "All timestamps are ISO 8601 with an explicit UTC offset and are returned in UTC.",
    )
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    register_error_handlers(app)
    app.include_router(slots.router)
    app.include_router(bookings.router)
    app.include_router(health.router)
    return app
