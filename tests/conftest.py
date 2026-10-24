import os
import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from clinic_booking.db import Base, create_db_engine, create_session_factory
from clinic_booking.main import create_app

# Set TEST_DATABASE_URL to run the whole suite against PostgreSQL. The database is
# wiped before every test, so never point it at a database holding real data.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def pytest_collection_modifyitems(config, items):
    if TEST_DATABASE_URL and TEST_DATABASE_URL.startswith("postgresql"):
        return
    # CI sets REQUIRE_POSTGRES so a misconfigured database URL fails the job instead
    # of silently skipping the PostgreSQL concurrency tests.
    if os.environ.get("REQUIRE_POSTGRES"):
        raise pytest.UsageError("REQUIRE_POSTGRES is set but TEST_DATABASE_URL is not PostgreSQL")
    skip = pytest.mark.skip(reason="needs TEST_DATABASE_URL pointing at PostgreSQL")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def database_url(tmp_path) -> str:
    return TEST_DATABASE_URL or f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    engine = create_db_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture
def app(engine: Engine) -> FastAPI:
    return create_app(engine=engine)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def live_server(app: FastAPI) -> Iterator[str]:
    """Run the app in a real uvicorn server so concurrent tests send genuinely
    parallel HTTP requests, each handled on its own worker thread and DB session."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    sock.close()
