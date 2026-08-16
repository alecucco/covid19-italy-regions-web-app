"""Shared test configuration: a temporary SQLite database, populated
with fake, controlled data -- no real network calls, no dependency on
the real 262,807 records, so tests stay fast and repeatable anywhere."""

import datetime
import os
import tempfile
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_DB", "test")
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")

import app.database as database_module

# Path in a temp folder, without opening a file descriptor ourselves:
# SQLite will create the file on first connection.
_tmp_db_path = os.path.join(tempfile.gettempdir(), f"test_covid_{uuid.uuid4().hex}.db")
test_engine = create_engine(f"sqlite:///{_tmp_db_path}")
database_module.engine = test_engine
database_module.SessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)

from app.database import Base, SessionLocal  # noqa: E402
from app.models import Province, ProvinceCase, Region  # noqa: E402

Base.metadata.create_all(bind=test_engine)

# Fake data: 3 regions, 3 provinces (one per region), 2 days.
# Names chosen deliberately so they're never confused with real regions.
TEST_DATE_OLD = datetime.date(2020, 3, 1)
TEST_DATE_LATEST = datetime.date(2020, 3, 2)

with SessionLocal() as session:
    session.add_all(
        [
            Region(region_code=1, region_name="TestRegionA", nuts_1_code=None, nuts_2_code=None),
            Region(region_code=2, region_name="TestRegionB", nuts_1_code=None, nuts_2_code=None),
            Region(region_code=3, region_name="TestRegionZ", nuts_1_code=None, nuts_2_code=None),
        ]
    )
    session.add_all(
        [
            Province(province_code=1, province_name="ProvA", province_abbreviation="PA",
                      region_code=1, latitude=0.0, longitude=0.0, nuts_3_code=None),
            Province(province_code=2, province_name="ProvB", province_abbreviation="PB",
                      region_code=2, latitude=0.0, longitude=0.0, nuts_3_code=None),
            Province(province_code=3, province_name="ProvZ", province_abbreviation="PZ",
                      region_code=3, latitude=0.0, longitude=0.0, nuts_3_code=None),
        ]
    )
    # On the most recent day: A=300, B=100, Z=200 -- deliberately distinct
    # values, so the order by cases and the order by name are always
    # different from each other, making sort tests unambiguous.
    session.add_all(
        [
            ProvinceCase(date=TEST_DATE_OLD, province_code=1, total_cases=150, notes=None),
            ProvinceCase(date=TEST_DATE_OLD, province_code=2, total_cases=50, notes=None),
            ProvinceCase(date=TEST_DATE_OLD, province_code=3, total_cases=100, notes=None),
            ProvinceCase(date=TEST_DATE_LATEST, province_code=1, total_cases=300, notes=None),
            ProvinceCase(date=TEST_DATE_LATEST, province_code=2, total_cases=100, notes=None),
            ProvinceCase(date=TEST_DATE_LATEST, province_code=3, total_cases=200, notes=None),
        ]
    )
    session.commit()


@pytest.fixture(scope="session")
def db_session():
    """A ready-to-use database session, already populated with the fake data."""
    with SessionLocal() as session:
        yield session


@pytest.fixture(scope="session")
def client():
    """FastAPI TestClient, with network ingestion disabled (the fake
    data is already present, it should never try to contact GitHub)."""
    with patch("app.main.needs_ingestion", return_value=False), \
         patch("app.main.check_for_updates", return_value=False):
        from fastapi.testclient import TestClient
        from app.main import app as fastapi_app

        with TestClient(fastapi_app) as test_client:
            yield test_client


def pytest_sessionfinish(session, exitstatus):
    """Clean up the temporary database file after the whole test session."""
    # Explicitly dispose of the SQLAlchemy connection pool: on Windows,
    # a file can't be deleted while something still has it open (unlike
    # Linux, where this problem doesn't come up).
    test_engine.dispose()
    try:
        os.remove(_tmp_db_path)
    except OSError:
        # Best-effort cleanup: if the file stays locked for some reason,
        # it shouldn't make a successful test session look like it failed --
        # the operating system will clean up the temp folder eventually.
        pass